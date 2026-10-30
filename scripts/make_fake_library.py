"""Generate a fake photo library for performance testing (PRD §10).

Writes small JPEGs with varied EXIF dates into year/month folders, a share of exact
duplicate copies, some PNGs without EXIF, a few corrupt files, and non-image files.
The synthetic Takeout layout with JSON sidecars is V1 work and not generated yet.

Usage: uv run python scripts/make_fake_library.py OUT_DIR [--count 100000]
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import random
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

START = datetime(2005, 1, 1)
SPAN_SECONDS = int((datetime(2026, 1, 1) - START).total_seconds())


def _make_one(args: tuple[str, int, int]) -> str:
    out, index, seed = args
    rng = random.Random(seed)
    taken = START + timedelta(seconds=rng.randrange(SPAN_SECONDS))
    folder = Path(out) / f"{taken:%Y}" / f"{taken:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    w, h = rng.choice([(640, 480), (480, 640), (800, 600), (600, 400)])
    img = Image.new("RGB", (w, h), tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        box = (x0, y0, x0 + rng.randrange(20, 200), y0 + rng.randrange(20, 200))
        draw.ellipse(box, fill=tuple(rng.randrange(256) for _ in range(3)))
    draw.text((10, 10), f"#{index} {taken:%Y-%m-%d %H:%M}", fill=(255, 255, 255))
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--duplicates", type=float, default=0.05, help="share of exact copies")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    unique = int(args.count * (1 - args.duplicates))
    started = time.perf_counter()
    jobs = [(str(out), i, args.seed * 1_000_003 + i) for i in range(unique)]
    workers = max(1, (os.cpu_count() or 2) - 1)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
        made = list(pool.map(_make_one, jobs, chunksize=256))

    rng = random.Random(args.seed)
    copies = out / "Copies"
    copies.mkdir(exist_ok=True)
    for n in range(args.count - unique):
        src = Path(rng.choice(made))
        shutil.copy2(src, copies / f"copy{n}_{src.name}")

    broken = out / "Broken"
    broken.mkdir(exist_ok=True)
    for n in range(10):
        (broken / f"corrupt_{n}.jpg").write_bytes(os.urandom(2048))
    for n in range(20):
        (out / "Videos").mkdir(exist_ok=True)
        (out / "Videos" / f"VID_{n}.mp4").write_bytes(b"\x00" * 1024)
    elapsed = time.perf_counter() - started
    print(f"Wrote {args.count:,} images (+10 corrupt, 20 videos) to {out} in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
