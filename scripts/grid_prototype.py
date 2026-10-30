"""V1 grid prototype (PRD §8.6, risk in §17): 100,000 fake photos with month headers.

Opens ``PhotoGrid`` on in-memory items spread over about 20 years of months, with
placeholder thumbnails generated in memory. No files or database are touched.

Usage:
    uv run python scripts/grid_prototype.py              # interactive window
    uv run python scripts/grid_prototype.py --bench      # print timings and exit
    QT_QPA_PLATFORM=offscreen uv run python scripts/grid_prototype.py --bench

``--cold`` loads every placeholder through ``PooledThumbnailProvider`` on pool threads,
as real thumbnails would be, instead of serving them from a ready in-memory set.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from imagory.ui.grid_layout import GridLayout, MonthKey, build_sections  # noqa: E402
from imagory.ui.photo_grid import (  # noqa: E402
    CELL_SIZE,
    HEADER_HEIGHT,
    ICON_SIZE,
    SPACING,
    PhotoGrid,
    PooledThumbnailProvider,
    ThumbnailProvider,
)

PALETTE_SIZE = 64


def fake_month_counts(total: int, years: int = 20, seed: int = 1) -> list[tuple[MonthKey, int]]:
    """Uneven counts per month over ``years`` years ending October 2026, ~1% undated."""
    rng = random.Random(seed)
    months = [(2026 - (i + 2) // 12, (9 - i) % 12 + 1) for i in range(years * 12)]
    unknown = total // 100
    weights = [rng.uniform(0.1, 3.0) * (1 + i / len(months)) for i in range(len(months))]
    scale = (total - unknown) / sum(weights)
    counts = [int(w * scale) for w in weights]
    counts[0] += total - unknown - sum(counts)
    return [*zip(months, counts, strict=True), (None, unknown)]


class FakeSource:
    def __init__(self, counts: list[tuple[MonthKey, int]]) -> None:
        self.counts = counts

    def month_counts(self):
        return self.counts

    def item(self, index: int) -> int:
        return index

    def tooltip(self, index: int) -> str:
        return f"Fake photo {index:,}"


def placeholder_image(index: int) -> QImage:
    """A coloured landscape, portrait or square tile with the index written on it."""
    shape = index % 3
    w, h = (ICON_SIZE, ICON_SIZE * 3 // 4) if shape == 0 else (ICON_SIZE * 3 // 4, ICON_SIZE)
    if shape == 2:
        w = h = ICON_SIZE
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor.fromHsv((index * 37) % 360, 120, 200))
    painter = QPainter(image)
    painter.drawText(image.rect(), 0x84, str(index))  # AlignCenter
    painter.end()
    return image


class WarmProvider(ThumbnailProvider):
    """Every thumbnail already cached: measures pure painting cost."""

    def __init__(self) -> None:
        super().__init__()
        self._pixmaps = [QPixmap.fromImage(placeholder_image(i)) for i in range(PALETTE_SIZE)]

    def thumbnail(self, index: int, item: object) -> QPixmap:
        return self._pixmaps[index % PALETTE_SIZE]


def make_provider(cold: bool) -> ThumbnailProvider:
    if cold:
        return PooledThumbnailProvider(lambda item: placeholder_image(item), key=lambda item: item)
    return WarmProvider()


class TimedGrid(PhotoGrid):
    """Records how long each paint takes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.paint_ms: list[float] = []
        self.first_paint_at: float | None = None

    def paintEvent(self, event) -> None:
        t0 = time.perf_counter()
        super().paintEvent(event)
        now = time.perf_counter()
        self.paint_ms.append((now - t0) * 1000)
        if self.first_paint_at is None:
            self.first_paint_at = now


def wait_until(app: QApplication, condition, timeout_ms: int = 10_000) -> None:
    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: condition() and loop.quit())
    poll.start(1)
    QTimer.singleShot(timeout_ms, loop.quit)
    if not condition():
        loop.exec()
    poll.stop()


def summary(values: list[float]) -> str:
    values = sorted(values)
    return (
        f"median {statistics.median(values):.2f} ms, "
        f"p95 {values[int(len(values) * 0.95)]:.2f} ms, max {values[-1]:.2f} ms"
    )


def bench(app: QApplication, total: int, cold: bool) -> None:
    counts = fake_month_counts(total)

    t0 = time.perf_counter()
    for _ in range(100):
        sections = build_sections(counts)
        layout = GridLayout(sections, 1264, CELL_SIZE, SPACING, HEADER_HEIGHT)
    build_ms = (time.perf_counter() - t0) * 10
    t0 = time.perf_counter()
    for width in range(400, 2400, 10):
        layout.reflow(width)
    reflow_ms = (time.perf_counter() - t0) * 1000 / 200
    rng = random.Random(2)
    offsets = [rng.randrange(layout.height) for _ in range(10_000)]
    t0 = time.perf_counter()
    for y in offsets:
        layout.visible(y, y + 800)
    lookup_us = (time.perf_counter() - t0) * 1e6 / len(offsets)
    t0 = time.perf_counter()
    for y in offsets:
        layout.item_at(300, y)
    hit_us = (time.perf_counter() - t0) * 1e6 / len(offsets)
    print(f"Items: {layout.count:,} in {len(sections)} sections")
    print(f"Layout build (sections + positions): {build_ms:.3f} ms")
    print(f"Reflow to a new width: {reflow_ms:.3f} ms")
    print(f"Visible-range lookup: {lookup_us:.1f} µs; hit-test: {hit_us:.1f} µs")

    started = time.perf_counter()
    grid = TimedGrid()
    provider = make_provider(cold)
    grid.set_provider(provider)
    grid.resize(1280, 800)
    grid.set_data_source(FakeSource(counts))
    grid.show()
    wait_until(app, lambda: grid.first_paint_at is not None)
    first_paint = (grid.first_paint_at or time.perf_counter()) - started
    first, end = grid.visible_range()
    print(
        f"Widget to first paint: {first_paint * 1000:.1f} ms "
        f"({end - first} cells, {grid.grid_layout.columns} columns, "
        f"{grid.viewport().width()}x{grid.viewport().height()} viewport)"
    )
    if cold:
        assert isinstance(provider, PooledThumbnailProvider)
        wait_until(app, lambda: all(provider._cache.get(i) for i in range(first, end)))
        print(f"First screen of thumbnails loaded: {(time.perf_counter() - started) * 1000:.1f} ms")

    # Every step forces a full viewport repaint (an upper bound: real scrolling repaints the
    # same area). Offscreen Qt skips the compositor, so judge smoothness by eye as well.
    bar = grid.verticalScrollBar()
    pitch = grid.grid_layout.pitch
    patterns = (
        ("wheel, 1/3 row per frame", lambda i: i * pitch // 3, 600),
        ("page, 1 screen per frame", lambda i: i * bar.pageStep(), 300),
        ("random jumps across 20 years", lambda i: rng.randrange(bar.maximum() + 1), 300),
    )
    for label, position, frames in patterns:
        grid.paint_ms.clear()
        step_ms = []
        for i in range(frames):
            t0 = time.perf_counter()
            bar.setValue(min(position(i), bar.maximum()))
            grid.viewport().repaint()
            app.processEvents()
            step_ms.append((time.perf_counter() - t0) * 1000)
        print(f"Scroll {label} ({frames} steps): step {summary(step_ms)}")
        print(f"    paint only: {summary(grid.paint_ms)}")

    resize_ms = []
    for width in (1000, 1600, 900, 1920, 1280):
        t0 = time.perf_counter()
        grid.resize(width, 800)
        grid.viewport().repaint()
        resize_ms.append((time.perf_counter() - t0) * 1000)
    print(f"Resize + repaint: {summary(resize_ms)}")

    t0 = time.perf_counter()
    grid.select_all()
    print(f"Ctrl+A on {grid.count():,} items: {(time.perf_counter() - t0) * 1000:.2f} ms")
    if isinstance(provider, PooledThumbnailProvider):
        provider.cancel_pending()
        provider.wait()
    grid.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bench", action="store_true", help="print timings and exit")
    parser.add_argument("--cold", action="store_true", help="load thumbnails on pool threads")
    parser.add_argument("--count", type=int, default=100_000)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv)
    print(f"Qt platform: {app.platformName()}")
    if args.bench:
        bench(app, args.count, args.cold)
        return
    grid = PhotoGrid()
    grid.setWindowTitle(f"Imagory grid prototype ({args.count:,} fake photos)")
    grid.set_provider(make_provider(args.cold))
    grid.resize(1280, 800)
    grid.set_data_source(FakeSource(fake_month_counts(args.count)))
    grid.activated.connect(lambda index: print(f"activated {index:,}"))
    grid.show()
    app.exec()


if __name__ == "__main__":
    main()
