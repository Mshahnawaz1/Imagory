"""The database side of the photo grid (PRD §8.6): rows, month counts, and thumbnails.

``PhotoGrid`` (``photo_grid.py``) knows nothing about the database. This module gives it:

- ``PhotoDataSource``, a ``GridDataSource`` over grid rows. The first screen is queried
  directly, the rest on a background read-only connection (``grid_query.py``), so the
  first screen shows within a second at 100,000 photos (PRD §10). Photos a running scan
  commits are inserted in grid order. Rows and month counts come from injected callables,
  so a filtered or album view can reuse it.
- ``PhotoThumbnails``, a thumbnail provider with a size-limited memory cache that loads
  cached thumbnails off the UI thread and regenerates missing ones (they are disposable).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import cmp_to_key
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

from imagory import db, thumbs
from imagory.db import GridPhoto, Source
from imagory.ui.grid_layout import MonthKey, month_key
from imagory.ui.grid_query import GridQuery
from imagory.ui.photo_grid import ICON_SIZE, PooledThumbnailProvider

# Rows loaded synchronously: more than a 4K screen of thumbnails holds.
FIRST_PAGE = 500

RowsQuery = Callable[[sqlite3.Connection, int | None, int], list[GridPhoto]]
CountsQuery = Callable[[sqlite3.Connection], list[tuple[str | None, int]]]


def all_rows(conn: sqlite3.Connection, limit: int | None, offset: int) -> list[GridPhoto]:
    """Default rows: every visible photo (``db.grid_photos``)."""
    return db.grid_photos(conn, limit=limit, offset=offset)


def photo_matches(f: db.PhotoFilter, photo: GridPhoto) -> bool:
    """Whether a photo a scan just added belongs in the view of ``f``.

    Mirrors the text, date, source and folder filters of ``db.filtered_grid_photos`` on the
    row itself. Filters that need the database (duplicates, missing, album) reject the row;
    the reload after the scan settles it (people are linked by the scan thread, so a person
    filter rejects rows too).
    """
    if f.missing or f.has_duplicates or f.album_id is not None or f.person_id is not None:
        return False
    if f.source_ids and photo.source_id not in f.source_ids:
        return False
    if f.folder is not None:
        source_id, prefix = f.folder
        prefix = prefix.replace("\\", "/").strip("/")
        if photo.source_id != source_id:
            return False
        if prefix and not photo.relative_path.lower().startswith(prefix.lower() + "/"):
            return False
    path = photo.relative_path.lower()
    if any(word.lower() not in path for word in f.text.split()):
        return False
    if f.date_from or f.date_to:
        day = photo.taken_at_local[:10] if photo.taken_at_local else None
        if day is None or (f.date_from and day < f.date_from) or (f.date_to and day > f.date_to):
            return False
    return True


def view_queries(
    f: db.PhotoFilter,
) -> tuple[RowsQuery, CountsQuery, Callable[[GridPhoto], bool] | None]:
    """The rows query, month counts query and scan-row ``accepts`` for the filter ``f``.
    The empty filter uses the plain, faster queries."""
    if f.is_empty:
        return all_rows, db.month_counts, None
    return (
        lambda conn, limit, offset: db.filtered_grid_photos(conn, f, limit, offset),
        lambda conn: db.filtered_month_counts(conn, f),
        lambda photo: photo_matches(f, photo),
    )


def display_name(photo: GridPhoto) -> str:
    return photo.relative_path.rsplit("/", 1)[-1]


def format_date(taken_at_local: str | None) -> str:
    return taken_at_local.replace("T", " ") if taken_at_local else "Unknown date"


def sorts_before(a: GridPhoto, b: GridPhoto) -> bool:
    """Grid order, as in ``db.grid_photos``: newest first, no date last, then by path,
    then by source."""
    if a.taken_at_local != b.taken_at_local:
        if a.taken_at_local is None:
            return False
        if b.taken_at_local is None:
            return True
        return a.taken_at_local > b.taken_at_local
    # NOCASE in SQLite folds ASCII only; lower() can differ for other letters, and the
    # full reload after the scan settles any difference.
    a_path, b_path = a.relative_path.lower(), b.relative_path.lower()
    if a_path != b_path:
        return a_path < b_path
    return a.source_id < b.source_id


def insertion_index(photos: list[GridPhoto], photo: GridPhoto, lo: int = 0) -> int:
    """Where ``photo`` goes in the grid-ordered ``photos`` (after equal entries), searching
    from ``lo`` on."""
    hi = len(photos)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorts_before(photo, photos[mid]):
            hi = mid
        else:
            lo = mid + 1
    return lo


_grid_order = cmp_to_key(lambda a, b: -1 if sorts_before(a, b) else int(sorts_before(b, a)))


def month_counts_of(photos: Iterable[GridPhoto]) -> dict[MonthKey, int]:
    """Photos per grid section, counted from the rows themselves."""
    by_prefix = Counter(p.taken_at_local[:7] if p.taken_at_local else None for p in photos)
    counts: dict[MonthKey, int] = {}
    for prefix, n in by_prefix.items():
        key = month_key(prefix)
        counts[key] = counts.get(key, 0) + n
    return counts


def parse_month_counts(rows: Iterable[tuple[str | None, int]]) -> dict[MonthKey, int]:
    """``db.month_counts`` rows (``"YYYY-MM"`` or None) as grid section keys."""
    counts: dict[MonthKey, int] = {}
    for prefix, n in rows:
        key = month_key(prefix)
        counts[key] = counts.get(key, 0) + n
    return counts


class PhotoDataSource(QObject):
    """Grid rows from the database, for ``PhotoGrid``.

    ``photos`` holds the loaded rows in grid order and is only ever changed in place, so
    the viewer can keep a reference; grid index ``i`` is ``photos[i]`` once loaded.
    Until the background query finishes, items past the first page are unknown (None)
    and the month counts come from ``counts``; afterwards they are counted from the rows.

    ``rows(conn, limit, offset)`` and ``counts(conn)`` must give the same set of photos in
    ``db.grid_photos`` order. ``rows`` also runs on a pool thread with its own read-only
    connection, so it must not touch shared state. ``accepts`` decides whether a photo a
    scan just added belongs in this view (all do by default).
    """

    # The rest of the rows arrived; the month counts may have changed. Carries the indexes
    # of photos a scan added meanwhile that were held back (for PhotoGrid.items_inserted).
    completed = Signal(list)

    def __init__(
        self,
        conn: sqlite3.Connection,
        db_file: Path,
        *,
        rows: RowsQuery = all_rows,
        counts: CountsQuery = db.month_counts,
        accepts: Callable[[GridPhoto], bool] | None = None,
        first_page: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self.rows_query = rows
        self.counts_query = counts
        self.accepts = accepts
        self.first_page = first_page
        self.photos: list[GridPhoto] = []
        self._ids: set[int] = set()
        self._index: dict[int, int] | None = {}  # photo id -> index; None until rebuilt
        self._counts: dict[MonthKey, int] = {}
        self._pending: dict[int, GridPhoto] = {}  # scan rows past the loaded rows
        self._recount = False
        self._complete = True
        self._token = 0
        self._query = GridQuery(db_file, self)
        self._query.loaded.connect(self._on_rest)

    # Loading -----------------------------------------------------------------------

    def load(self) -> None:
        """(Re)read from the database: the first page now, the rest in the background."""
        self._token += 1
        page = self.first_page if self.first_page is not None else FIRST_PAGE
        with db.transaction(self._conn):  # one snapshot for the rows and the counts
            first = self.rows_query(self._conn, page, 0)
            complete = len(first) < page
            counts = None if complete else self.counts_query(self._conn)
        self._replace(first)
        self._pending.clear()
        self._recount = False
        self._complete = complete
        self._counts = month_counts_of(first) if counts is None else parse_month_counts(counts)
        if not complete:
            offset, token, query = len(first), self._token, self.rows_query
            self._query.start(lambda conn: query(conn, None, offset), token)

    def clear(self) -> None:
        """Show nothing, e.g. while a source is removed."""
        self._token += 1
        self._query.cancel()
        self._replace([])
        self._pending.clear()
        self._counts = {}
        self._complete = True

    @property
    def complete(self) -> bool:
        return self._complete

    def wait(self) -> None:
        """Block until a background query has finished (shutdown)."""
        self._query.wait()

    def _replace(self, photos: list[GridPhoto]) -> None:
        # A new list rather than in place: the viewer is rebound once its photo is found.
        self.photos = photos
        self._ids = {p.photo_id for p in photos}
        self._index = None

    def _on_rest(self, token: int, rest: list) -> None:
        if token != self._token:
            return  # reloaded in the meantime
        # A scan may have shown some already, or committed between the two snapshots.
        ids = self._ids
        rest = [p for p in rest if p.photo_id not in ids]
        start = len(self.photos)
        self.photos.extend(rest)  # in place: the viewer may hold this list
        self._ids.update(p.photo_id for p in rest)
        if self._index is not None:
            self._index.update((p.photo_id, start + i) for i, p in enumerate(rest))
        pending = [p for p in self._pending.values() if p.photo_id not in self._ids]
        self._pending.clear()
        self._complete = True
        positions = self._insert_sorted(pending, count=False)
        if self._recount or sum(self._counts.values()) != len(self.photos):
            self._counts = month_counts_of(self.photos)
        self._recount = False
        self.completed.emit(positions)

    # Photos a scan adds ------------------------------------------------------------

    def insert(self, photos: Sequence[GridPhoto]) -> list[int]:
        """Add photos a running scan committed, each at its place in grid order.

        Returns the indexes they now have (ascending, after all insertions), for
        ``PhotoGrid.items_inserted``. Photos already shown are ignored. Before the rest
        of the rows has loaded, photos that sort after the loaded rows wait until it has.
        """
        new = [
            p
            for p in photos
            if p.photo_id not in self._ids
            and p.photo_id not in self._pending
            and (self.accepts is None or self.accepts(p))
        ]
        if not new:
            return []
        new.sort(key=_grid_order)
        if not self._complete:
            self._recount = True
            loaded = []
            for p in new:
                if insertion_index(self.photos, p) >= len(self.photos):
                    self._pending[p.photo_id] = p  # its place is not loaded yet
                else:
                    loaded.append(p)
                key = month_key(p.taken_at_local)
                self._counts[key] = self._counts.get(key, 0) + 1
            return self._insert_sorted(loaded, count=False)
        return self._insert_sorted(new)

    def _insert_sorted(self, photos: list[GridPhoto], count: bool = True) -> list[int]:
        """Insert grid-ordered ``photos``; ascending insertion makes each index final."""
        positions = []
        lo = 0
        for p in photos:
            row = insertion_index(self.photos, p, lo)
            self.photos.insert(row, p)  # in place: the viewer holds this list
            self._ids.add(p.photo_id)
            positions.append(row)
            lo = row + 1
            if count:
                key = month_key(p.taken_at_local)
                self._counts[key] = self._counts.get(key, 0) + 1
        if positions:
            self._index = None  # indexes after each insertion point moved
        return positions

    # GridDataSource ----------------------------------------------------------------

    def month_counts(self) -> list[tuple[MonthKey, int]]:
        return list(self._counts.items())

    def count(self) -> int:
        return sum(self._counts.values())

    def item(self, index: int) -> GridPhoto | None:
        return self.photos[index] if 0 <= index < len(self.photos) else None

    def caption(self, index: int) -> str:
        photo = self.item(index)
        return display_name(photo) if photo is not None else ""

    def tooltip(self, index: int) -> str:
        photo = self.item(index)
        if photo is None:
            return ""
        return f"{photo.relative_path}\n{format_date(photo.taken_at_local)}"

    def index_of(self, photo_id: int) -> int | None:
        if self._index is None:
            self._index = {p.photo_id: i for i, p in enumerate(self.photos)}
        return self._index.get(photo_id)


def thumbnail_file(thumbs_root: Path, source: Source, photo: GridPhoto) -> Path | None:
    """The cached thumbnail of ``photo``, regenerated from its file if it is missing.

    The grid's only thumbnail lookup, so a change of thumbnail keys (content hash,
    PRD §8.5) changes this one function. Runs on a loader thread; reads the photo only.
    """
    return thumbs.ensure_thumbnail(
        Path(source.root_path), thumbs_root, photo.relative_path, photo.content_hash
    )


def load_thumbnail(path: Path, size: int) -> QImage:
    """Decode a cached thumbnail at display size, almost entirely in C++.

    Qt releases the GIL while reading, so loader threads barely stall the UI thread.
    """
    reader = QImageReader(str(path))
    original = reader.size()
    if original.isValid() and max(original.width(), original.height()) > size:
        reader.setScaledSize(original.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return image
    # RGB32 is the raster engine's native format, so painting needs no conversion.
    return image.convertToFormat(QImage.Format.Format_RGB32)


class PhotoThumbnails(PooledThumbnailProvider):
    """Thumbnails of ``GridPhoto`` items, cached in memory by photo id.

    ``sources`` maps source ids to sources and is read on the UI thread only, when a
    load is queued; the loader thread gets the source it needs with the task.
    """

    def __init__(
        self,
        thumbs_root: Path,
        sources: Mapping[int, Source],
        *,
        device_pixel_ratio: float = 1.0,
        cache_items: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        self.thumbs_root = thumbs_root
        self.pixel_size = round(ICON_SIZE * device_pixel_ratio)
        extra = {} if cache_items is None else {"cache_items": cache_items}
        super().__init__(
            self._load_item,
            _photo_id,
            device_pixel_ratio=device_pixel_ratio,
            parent=parent,
            **extra,
        )
        self._sources = sources

    def thumbnail(self, index: int, item: object) -> QPixmap | None:
        if not isinstance(item, GridPhoto):
            return None  # not loaded yet
        source = self._sources.get(item.source_id)
        if source is None:
            return None  # its source is being removed
        return super().thumbnail(index, (item, source))

    def _load_item(self, item: object) -> QImage:
        photo, source = item  # type: ignore[misc]
        path = thumbnail_file(self.thumbs_root, source, photo)
        return load_thumbnail(path, self.pixel_size) if path else QImage()


def _photo_id(item: object) -> int:
    return item[0].photo_id  # type: ignore[index]
