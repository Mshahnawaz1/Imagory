"""Measure V0 performance against a (fake) library (PRD §7.5 item 6).

Runs a full scan and an unchanged rescan with a fresh library, then opens the main window
offscreen to time the first screen of thumbnails and the cost of scrolling.

Usage:
    IMAGORY_LIBRARY=.bench-library uv run python scripts/bench.py SOURCE_DIR [--skip-scan]
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imagory import db, paths  # noqa: E402
from imagory.scanner import Scanner  # noqa: E402


def scan_once(conn, source, root) -> tuple[float, db.ScanCounts]:
    started = time.perf_counter()
    last = [0.0]

    def progress(p):
        now = time.perf_counter()
        if p.phase == "processing" and now - last[0] > 10:
            last[0] = now
            print(f"  {p.processed:,}/{p.to_process:,} ({now - started:.0f}s)", flush=True)

    summary = Scanner(conn, source, paths.thumbs_dir(root), root, progress=progress).run()
    return time.perf_counter() - started, summary.counts


def wait_until(condition, timeout_ms: int = 30_000) -> None:
    """Wait inside a real event loop, like the app does, instead of a busy Python loop
    that would hold the GIL and starve background threads."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: condition() and loop.quit())
    poll.start(5)
    QTimer.singleShot(timeout_ms, loop.quit)
    if not condition():
        loop.exec()
    poll.stop()


def bench_ui(root: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    t0 = time.perf_counter()
    from PySide6.QtWidgets import QApplication

    from imagory.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    print(f"Qt and UI imports: {time.perf_counter() - t0:.3f}s")
    started = time.perf_counter()
    conn = db.open_database(paths.db_path(root))
    window = MainWindow(root, conn)
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    model_ready = time.perf_counter() - started

    def visible_rows() -> list[int]:
        view = window.grid
        rows = []
        for row in range(window.model.rowCount()):
            rect = view.visualRect(window.model.index(row))
            if rect.top() > view.viewport().height():
                break
            if rect.intersects(view.viewport().rect()):
                rows.append(row)
        return rows

    first_rows = visible_rows()
    photos = window.model.photos
    wait_until(lambda: all(window.loader._cache.get(photos[r].photo_id) for r in first_rows))
    first_screen = time.perf_counter() - started
    print(f"Model loaded ({len(photos):,} photos) in {model_ready:.3f}s")
    print(f"First screen ({len(first_rows)} thumbnails) visible in {first_screen:.3f}s")

    total = conn.execute("SELECT COUNT(*) FROM files WHERE status = 'ok'").fetchone()[0]
    wait_until(lambda: window.model.rowCount() >= total, timeout_ms=120_000)
    print(
        f"Whole grid ({window.model.rowCount():,} photos) loaded in "
        f"{time.perf_counter() - started:.3f}s (in the background)"
    )

    # Scroll cost, as an upper bound: every step forces a full viewport repaint, while real
    # scrolling only repaints the newly exposed strip. Thumbnail loads run concurrently.
    # Offscreen Qt does not paint on its own, so smoothness must still be judged by eye on
    # a real display. 16 ms per frame is 60 fps.
    bar = window.grid.verticalScrollBar()
    step = window.grid.gridSize().height()
    patterns = (
        ("wheel (1/3 row per frame)", lambda i: i * step // 3, 300),
        ("jumps (7 rows per frame)", lambda i: (i * step * 7) % max(1, bar.maximum()), 100),
    )
    for label, position, frames_count in patterns:
        frames = []
        for i in range(frames_count):
            t0 = time.perf_counter()
            bar.setValue(position(i))
            window.grid.viewport().repaint()
            app.processEvents()
            frames.append((time.perf_counter() - t0) * 1000)
        frames.sort()
        print(
            f"Scroll {label}: median {statistics.median(frames):.1f} ms, "
            f"p95 {frames[int(len(frames) * 0.95)]:.1f} ms, max {frames[-1]:.1f} ms"
        )
    window.close()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--skip-scan", action="store_true", help="only run the UI timings")
    args = parser.parse_args()
    if not os.environ.get(paths.LIBRARY_ENV):
        sys.exit("Set IMAGORY_LIBRARY to a scratch folder first.")
    root = paths.ensure_library_dir()

    if not args.skip_scan:
        conn = db.open_database(paths.db_path(root))
        if db.get_source(conn) is not None:
            sys.exit(f"{root} already has a source; use a fresh IMAGORY_LIBRARY.")
        source = db.create_source(conn, str(args.source.resolve()), args.source.name)
        print("Full scan…")
        seconds, c = scan_once(conn, source, root)
        print(f"Full scan: {seconds:.1f}s, found {c.found:,}, added {c.added:,}, errors {c.errors}")
        print("Unchanged rescan…")
        seconds, c = scan_once(conn, source, root)
        print(
            f"Unchanged rescan: {seconds:.2f}s, unchanged {c.unchanged:,}, "
            f"added {c.added}, updated {c.updated}, errors {c.errors}"
        )
        conn.close()
    bench_ui(root)


if __name__ == "__main__":
    main()
