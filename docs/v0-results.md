# V0 results (PRD §7.5)

Recorded 2026-10-31.

## Checklist

| # | Item | Result |
|---|------|--------|
| 1 | Unit tests: walk rules, scan diff, date fallback, migrations | Pass (`tests/test_walk.py`, `test_diff.py`, `test_metadata.py`, `test_db.py`) |
| 2 | Integration: rows checked; unchanged rescan processes 0; modify + delete gives 1 updated, 1 missing | Pass (`tests/test_scan.py::test_full_scan_rescan_modify_delete`) |
| 3 | Cancel mid-scan, rescan: no duplicate rows, committed files not reprocessed | Pass (`test_cancel_then_resume`, `tests/test_ui.py::test_cancel_scan_from_ui`) |
| 4 | Corrupt image counted as error, scan continues | Pass (`test_corrupt_image_is_counted_and_scan_continues`) |
| 5 | Source folder unchanged after a scan | Pass (snapshot checks in every scan integration test) |
| 6 | 100,000-photo library | Measured on Linux/WSL, see below. **Scrolling smoothness still needs a by-eye check on Windows.** |
| 7 | PyInstaller `--onefile` .exe on Windows 10 and 11, scan works, size recorded | **Not done: needs real Windows.** The spec was validated with a Linux onefile build, which starts and creates its library (77.8 MB, but Linux Qt libraries are not comparable to Windows). |

Full suite: 52 passed, 1 skipped (the junction test runs only on Windows).

## 100,000-photo run

Machine: AMD Ryzen 3 5300U (8 threads), WSL2 on an SSD (ext4), 7.6 GB RAM with the system
already swapping, and the VS Code server watching the workspace during the scan. So these
numbers are pessimistic.

Library: `scripts/make_fake_library.py --count 100000`: 95,000 unique JPEG/PNG images
(640×480 to 800×600) with EXIF dates from 2005 to 2025, 5,000 exact copies, 10 corrupt files,
and 20 videos. 1.3 GB in total.

| Measurement | Result | Target |
|---|---|---|
| Full scan (metadata + thumbnail, 7 workers) | 221 s (about 450 files/s), 100,000 added, 10 errors | runs in background, resumable |
| Unchanged rescan (walk + diff) | 5.4 s | < 60 s |
| First screen of thumbnails after relaunch | 0.20 s from window creation, plus 0.23–0.47 s for Qt/UI imports and about 0.1 s for interpreter start | < 1 s |
| Whole grid (100,000 rows) loaded in the background | 1.5 s | — |
| Thumbnail cache size | 891 MB | — |
| Database size | 35 MB | — |

Scrolling (offscreen Qt, forced full-viewport repaint per step, so an upper bound):

| Pattern | Median | p95 | Max |
|---|---|---|---|
| Wheel-like, 1/3 row per frame | 20.7 ms | 34.7 ms | 50.1 ms |
| Jumps, 7 rows per frame (all thumbnails new) | 77.4 ms | 100.0 ms | 120.2 ms |

Offscreen Qt only paints when forced, so these numbers can't show what scrolling feels
like. Real scrolling repaints only the newly exposed strip. **Smooth or not: to be recorded
by eye on Windows.**

Unchanged rescans still retry the 10 corrupt files, which starts the worker pool. Unreadable
files are not stored, so a repaired file is picked up (PRD §16).

## Fixes made while measuring

- JPEG draft mode was never reducing: `Image.draft` needs both sides at or above the
  requested size, so a 320×320 request skipped most photos. Requesting the fitted size took
  thumbnail work from about 47 ms to 3.4 ms per file.
- Grid painting: a custom delegate paints prepared pixmaps instead of the default
  QIcon conversion, and thumbnails decode in C++ (`QImageReader`) on two loader threads.
- First screen: the grid queries the first 500 rows (index-ordered, 4–8 ms) and loads the
  rest on a background reader thread. Loading all 100,000 rows synchronously took 1.3–2 s.

## Still to do on Windows

1. `uv run pyinstaller --noconfirm --clean imagory.spec` on Windows. Record the size of
   `dist\Imagory.exe`.
2. Start the .exe on Windows 10 and on Windows 11, choose a folder, and let the scan finish.
   This checks `freeze_support` with worker processes.
3. Scroll the 100,000-photo grid and record whether it feels smooth.
4. Run `uv run pytest -q` on Windows (includes the junction test).
