"""Time the search and filter queries (PRD §8.7) on a synthetic database.

Builds a throwaway database with N photos (10,000 by default; use 100000 for the PRD size),
some missing files and some duplicates in a second source, then times each filter's first
page, all rows and month counts. Nothing outside a temporary folder is written.

Usage:
    uv run python scripts/bench_filters.py [N]
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imagory import db  # noqa: E402
from imagory.db import PhotoFilter  # noqa: E402


def add_source(conn, label: str) -> int:
    return conn.execute(
        "INSERT INTO sources (label, root_path, added_at) VALUES (?, ?, '2020-01-01T00:00:00Z')",
        (label, f"/x/{label}"),
    ).lastrowid


def fill(conn, n: int) -> tuple[int, int]:
    s1, s2 = add_source(conn, "A"), add_source(conn, "B")
    rnd = random.Random(1)
    photos, files = [], []
    for i in range(1, n + 1):
        y, m, d = rnd.randint(2005, 2025), rnd.randint(1, 12), rnd.randint(1, 28)
        taken = f"{y}-{m:02d}-{d:02d}T12:00:00" if i % 50 else None
        photos.append((i, f"h{i}", None, taken))
        folder = f"{rnd.randint(2005, 2025)}/Event {i % 300}"
        status = "missing" if i % 100 == 1 else "ok"
        files.append((i, s1, i, f"{folder}/IMG_{i:06d}.jpg", status))
        if i % 10 == 0:
            files.append((n + i, s2, i, f"Takeout/{folder}/IMG_{i:06d}.jpg", "ok"))
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO photos (id, content_hash, primary_file_id, taken_at_local) "
            "VALUES (?,?,?,?)",
            photos,
        )
        conn.executemany(
            "INSERT INTO files (id, source_id, photo_id, relative_path, size, modified_time, "
            "status, first_seen_at, last_seen_at) VALUES (?,?,?,?,1,1,?,'z','z')",
            files,
        )
        conn.execute("UPDATE photos SET primary_file_id = id")
    return s1, s2


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.open_database(Path(tmp) / "imagory.db")
        try:
            s1, s2 = fill(conn, n)
            cases = {
                "none": PhotoFilter(),
                "text": PhotoFilter(text="event 17"),
                "date": PhotoFilter(date_from="2020-01-01", date_to="2020-12-31"),
                "source": PhotoFilter(source_ids=frozenset({s2})),
                "folder": PhotoFilter(folder=(s1, "2015")),
                "dups": PhotoFilter(has_duplicates=True),
                "missing": PhotoFilter(missing=True),
                "combo": PhotoFilter(text="event", date_from="2010-01-01", has_duplicates=True),
            }
            print(f"{n:,} photos")
            for name, f in cases.items():
                t0 = time.perf_counter()
                db.filtered_grid_photos(conn, f, 200)
                t1 = time.perf_counter()
                rows = db.filtered_grid_photos(conn, f)
                t2 = time.perf_counter()
                db.filtered_month_counts(conn, f)
                t3 = time.perf_counter()
                print(
                    f"{name:8} rows={len(rows):7,} first200={t1 - t0:.3f}s "
                    f"all={t2 - t1:.3f}s months={t3 - t2:.3f}s"
                )
            t0 = time.perf_counter()
            db.folder_paths(conn, s1)
            print(f"folder_paths {time.perf_counter() - t0:.3f}s")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
