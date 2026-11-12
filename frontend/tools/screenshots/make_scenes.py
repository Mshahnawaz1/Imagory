"""Generate synthetic photos (drawn scenes, no real images) for the marketing screenshots.

Usage: uv run python frontend/tools/screenshots/make_scenes.py OUT_DIR

Writes OUT_DIR/Photos (a plain folder: year/month subfolders, a few exact duplicates)
and OUT_DIR/Takeout (a small fictional Google Takeout export with JSON sidecars,
fictional people, places and an album folder).
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASPECTS = [(4, 3), (3, 2), (16, 9), (3, 4), (2, 3), (1, 1)]
ASPECT_WEIGHTS = [4, 5, 2, 2, 1, 1]
LONG_EDGE = 1600

PALETTES = {
    "sunset": [(26, 20, 60), (120, 40, 110), (235, 100, 70), (255, 190, 100)],
    "dawn": [(40, 60, 120), (160, 120, 170), (250, 170, 150), (255, 225, 170)],
    "day": [(40, 120, 210), (110, 175, 235), (190, 225, 250), (240, 248, 255)],
    "dusk": [(10, 14, 40), (50, 40, 110), (150, 70, 130), (240, 130, 110)],
    "overcast": [(110, 125, 140), (150, 165, 175), (190, 200, 205), (225, 228, 228)],
    "golden": [(70, 110, 170), (200, 170, 150), (250, 190, 100), (255, 225, 150)],
}

PLACES = [
    (48.8566, 2.3522),
    (35.6762, 139.6503),
    (40.7128, -74.0060),
    (-33.8688, 151.2093),
    (52.5200, 13.4050),
    (46.2044, 6.1432),
    (64.1466, -21.9426),
    (41.3851, 2.1734),
]
PEOPLE = ["Alex Rivera", "Sam Lee", "Priya N.", "Jordan", "Casey Morgan", "Robin Ito"]
CAPTIONS = [
    "Golden hour at the lake",
    "Weekend trip",
    "Morning walk",
    "Dinner with friends",
    "First snow",
    "Harbour at dusk",
    "",
    "",
    "",
]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ramp(stops, t):
    t = min(max(t, 0), 0.9999) * (len(stops) - 1)
    i = int(t)
    return lerp(stops[i], stops[i + 1], t - i)


def sky(size, stops):
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=ramp(stops, 1 - y / h))
    return img


def ridge(rng, w, base, amp, rough=0.55, points=9):
    """A ridge line as y values per x, by midpoint displacement."""
    n = 2**points
    ys = [0.0] * (n + 1)
    ys[0], ys[n] = rng.uniform(-1, 1), rng.uniform(-1, 1)
    step, scale = n, 1.0
    while step > 1:
        half = step // 2
        for i in range(half, n, step):
            ys[i] = (ys[i - half] + ys[i + half]) / 2 + rng.uniform(-1, 1) * scale
        step, scale = half, scale * rough
    return [base + ys[int(x / w * n)] * amp for x in range(w)]


def fill_ridge(img, ys, colour):
    d = ImageDraw.Draw(img)
    w, h = img.size
    d.polygon([(0, h)] + [(x, y) for x, y in enumerate(ys)] + [(w - 1, h)], fill=colour)


def haze(img, strength=0.15, colour=(255, 230, 200)):
    return Image.blend(img, Image.new("RGB", img.size, colour), strength)


def sun(img, rng, stops):
    w, h = img.size
    glow = Image.new("RGB", img.size, (0, 0, 0))
    d = ImageDraw.Draw(glow)
    cx, cy = rng.uniform(0.2, 0.8) * w, rng.uniform(0.35, 0.65) * h
    r = h * 0.07
    d.ellipse([cx - r * 3, cy - r * 3, cx + r * 3, cy + r * 3], fill=(90, 60, 30))
    glow = glow.filter(ImageFilter.GaussianBlur(h * 0.08))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 245, 215))
    glow = glow.filter(ImageFilter.GaussianBlur(2))
    return ImageChops.add(img, glow)


def scene_mountains(rng, size):
    w, h = size
    stops = PALETTES[rng.choice(["sunset", "dawn", "day", "golden", "overcast"])]
    img = sun(sky(size, stops), rng, stops)
    far = ramp(stops, 0.45)
    for i in range(4):
        t = i / 3
        col = lerp(far, (14, 24, 40), 0.25 + 0.7 * t)
        fill_ridge(img, ridge(rng, w, h * (0.45 + 0.12 * i), h * (0.22 - 0.03 * i)), col)
        if i < 2:
            img = haze(img, 0.12, ramp(stops, 0.7))
    return img


def scene_sea(rng, size):
    w, h = size
    stops = PALETTES[rng.choice(["sunset", "dawn", "dusk", "golden"])]
    img = sun(sky(size, stops), rng, stops)
    d = ImageDraw.Draw(img)
    horizon = int(h * rng.uniform(0.5, 0.65))
    for y in range(horizon, h):
        t = (y - horizon) / (h - horizon)
        d.line([(0, y), (w, y)], fill=lerp(ramp(stops, 0.45), (8, 30, 55), 0.3 + 0.65 * t))
    for _ in range(260):
        y = rng.randrange(horizon, h)
        x = rng.randrange(-50, w)
        L = rng.randrange(20, 120) * (1 + (y - horizon) / h * 3)
        d.line([(x, y), (x + L, y)], fill=lerp(ramp(stops, 0.6), (255, 255, 255), 0.3), width=2)
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    if rng.random() < 0.6:  # a distant headland
        fill_ridge(img, ridge(rng, w, horizon, h * 0.04), (20, 30, 45))
    return img


def tree(d, x, y, s, col):
    d.rectangle([x - s * 0.06, y - s * 0.3, x + s * 0.06, y], fill=(30, 22, 18))
    for k in range(4):
        wy = y - s * (0.2 + 0.28 * k)
        ww = s * (0.5 - 0.1 * k)
        d.polygon([(x - ww, wy), (x + ww, wy), (x, wy - s * 0.45)], fill=col)


def scene_forest(rng, size):
    w, h = size
    stops = PALETTES[rng.choice(["dawn", "golden", "overcast", "day"])]
    img = sky(size, stops)
    d = ImageDraw.Draw(img)
    fill_ridge(img, ridge(rng, w, h * 0.55, h * 0.08), lerp(ramp(stops, 0.5), (20, 60, 50), 0.6))
    green = rng.choice([(24, 70, 50), (30, 80, 40), (20, 60, 70), (60, 80, 40)])
    for layer in range(5):
        t = layer / 4
        col = lerp(lerp(green, ramp(stops, 0.5), 0.5 - 0.45 * t), (5, 20, 15), 0.45 * t)
        y = h * (0.62 + 0.1 * layer)
        for _ in range(26 + 6 * layer):
            tree(d, rng.uniform(-0.05, 1.05) * w, y + rng.uniform(-8, 8), h * (0.16 + 0.1 * t), col)
        if layer < 3:
            img = haze(img, 0.1, ramp(stops, 0.7))
            d = ImageDraw.Draw(img)
    d.rectangle([0, h * 0.97, w, h], fill=(8, 22, 16))
    return img


def scene_city(rng, size):
    w, h = size
    stops = PALETTES[rng.choice(["dusk", "sunset", "dawn"])]
    img = sky(size, stops)
    d = ImageDraw.Draw(img)
    for layer in range(3):
        base = h * (0.72 + 0.04 * layer)
        col = lerp(ramp(stops, 0.35), (8, 10, 24), 0.4 + 0.25 * layer)
        x = -20
        while x < w:
            bw = rng.randrange(int(w * 0.03), int(w * 0.09))
            bh = rng.uniform(0.1, 0.38 - 0.06 * layer) * h
            d.rectangle([x, base - bh, x + bw, h], fill=col)
            if layer == 2:
                for wy in range(int(base - bh + 14), int(h), 22):
                    for wx in range(x + 8, x + bw - 8, 16):
                        if rng.random() < 0.4:
                            d.rectangle([wx, wy, wx + 6, wy + 9], fill=(255, 215, 130))
            x += bw + rng.randrange(0, 6)
        if layer < 2:
            img = haze(img, 0.12, ramp(stops, 0.6))
            d = ImageDraw.Draw(img)
    return img


def scene_bokeh(rng, size):
    w, h = size
    stops = rng.choice([PALETTES["dusk"], PALETTES["sunset"], PALETTES["dawn"]])
    img = sky(size, stops).filter(ImageFilter.GaussianBlur(30))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(rng.randrange(40, 90)):
        r = rng.uniform(0.02, 0.09) * min(w, h)
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        col = lerp(ramp(stops, rng.random()), (255, 255, 255), rng.uniform(0.1, 0.6))
        d.ellipse([x - r, y - r, x + r, y + r], fill=col + (rng.randrange(40, 120),))
        d.ellipse([x - r, y - r, x + r, y + r], outline=col + (170,), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    return img.filter(ImageFilter.GaussianBlur(1.5))


def scene_dunes(rng, size):
    w, h = size
    stops = PALETTES[rng.choice(["golden", "sunset", "day"])]
    img = sky(size, stops)
    for i in range(5):
        col = lerp((236, 176, 110), (90, 40, 40), i / 5)
        ys = ridge(rng, w, h * (0.55 + 0.09 * i), h * 0.07, rough=0.4, points=6)
        fill_ridge(img, ys, col)
    return img


SCENES = [scene_mountains, scene_sea, scene_forest, scene_city, scene_bokeh, scene_dunes]


def vignette(img):
    w, h = img.size
    mask = Image.radial_gradient("L").resize((w, h))
    dark = Image.new("RGB", img.size, (0, 0, 0))
    return Image.composite(dark, img, mask.point(lambda v: max(0, v - 150) // 2))


def make_photo(rng, size) -> Image.Image:
    scene = rng.choices(SCENES, weights=[4, 4, 3, 2, 1, 2])[0]
    base = (max(size) * 0.0 + 1) and scene(rng, size)
    return vignette(base)


def pick_size(rng):
    a, b = rng.choices(ASPECTS, ASPECT_WEIGHTS)[0]
    k = LONG_EDGE / max(a, b)
    return int(a * k), int(b * k)


def exif_bytes(taken: datetime) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Imagory Demo"
    exif[0x0110] = "Synthetic Camera"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x9004] = taken.strftime("%Y:%m:%d %H:%M:%S")
    return exif.tobytes()


def save(img, path: Path, taken: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=84, exif=exif_bytes(taken))


def random_time(rng, start, end):
    secs = int((end - start).total_seconds())
    t = start + timedelta(seconds=rng.randrange(secs))
    return t.replace(hour=rng.choice([7, 8, 9, 12, 14, 16, 18, 19, 20]), minute=rng.randrange(60))


def ts(dt: datetime) -> dict:
    u = int(dt.replace(tzinfo=UTC).timestamp())
    return {"timestamp": str(u), "formatted": dt.strftime("%b %d, %Y, %I:%M:%S %p UTC")}


def main(out: Path, count: int = 190, takeout_count: int = 48) -> None:
    rng = random.Random(7)
    if out.exists():
        shutil.rmtree(out)
    photos = out / "Photos"
    # Plain folder: clustered over ~2 years so the grid has several month headers.
    start, end = datetime(2024, 3, 1), datetime(2025, 10, 28)
    times = sorted(random_time(rng, start, end) for _ in range(count))
    made: list[Path] = []
    for i, t in enumerate(times):
        img = make_photo(rng, pick_size(rng))
        name = f"IMG_{t:%Y%m%d}_{i:04d}.jpg"
        p = photos / f"{t:%Y}" / f"{t:%m}" / name
        save(img, p, t)
        made.append(p)
    # Exact duplicates (backup copies in another folder)
    for p in rng.sample(made, 9):
        dest = photos / "Backup 2024" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)

    # Takeout
    gp = out / "Takeout" / "Google Photos"
    (out / "Takeout").mkdir(parents=True)
    (out / "Takeout" / "archive_browser.html").write_text("<!doctype html><title>Takeout</title>\n")
    album = gp / "Summer in the Alps"
    album.mkdir(parents=True)
    (album / "metadata.json").write_text(
        json.dumps({"title": "Summer in the Alps", "description": "", "access": "protected"})
    )
    tstart, tend = datetime(2025, 6, 1), datetime(2025, 11, 5)
    ttimes = sorted(random_time(rng, tstart, tend) for _ in range(takeout_count))
    for i, t in enumerate(ttimes):
        img = make_photo(rng, pick_size(rng))
        name = f"IMG_{4100 + i}.jpg"
        year_dir = gp / f"Photos from {t:%Y}"
        p = year_dir / name
        save(img, p, t)
        lat, lon = rng.choice(PLACES)
        lat += rng.uniform(-0.02, 0.02)
        lon += rng.uniform(-0.02, 0.02)
        side = {
            "title": name,
            "description": rng.choice(CAPTIONS),
            "imageViews": str(rng.randrange(30)),
            "creationTime": ts(t + timedelta(days=1)),
            "photoTakenTime": ts(t),
            "geoData": {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "altitude": 12.0,
                "latitudeSpan": 0.0,
                "longitudeSpan": 0.0,
            },
            "people": [{"name": n} for n in rng.sample(PEOPLE, rng.choice([0, 1, 1, 2, 3]))],
            "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}},
        }
        if rng.random() < 0.15:
            side["favorited"] = True
        (year_dir / f"{name}.supplemental-metadata.json").write_text(json.dumps(side, indent=2))
        if i % 3 == 0:  # also in the album as an exact copy
            shutil.copy2(p, album / name)
    print(f"wrote {count} + {len(list(gp.rglob('*.jpg')))} photos to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
