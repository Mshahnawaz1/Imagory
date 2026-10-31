# V1 wave 1: parallel agent plan

Five agents work in parallel, each on its own branch in its own git worktree. The lead
(main session) merges the branches into `main` once they are finished, then updates
`PRD.md`, `README.md`, and `tasks.md`. Scope comes from PRD §8; the task list is `tasks.md`.

## Assignments

| Agent | Task | PRD | Branch | Model |
| --- | --- | --- | --- | --- |
| A | Duplicates, photo identity, moved and missing files, date wiring | §8.5, §8.2, §8.4 | `feat/v1-duplicates` | Opus (cross-cutting, migration of V0 data) |
| B | Takeout sidecar matching, reading, and matching report (pure layer) | §8.3 | `feat/v1-takeout-sidecars` | Sonnet |
| C | Grid integration: `PhotoGrid` replaces `PhotoGridView` | §8.6 grid | `feat/v1-grid-integration` | Opus (UI integration, crash history) |
| D | Search and filters: query layer and filter bar widget | §8.7 | `feat/v1-search` | Sonnet |
| E | Albums: schema, queries, and dialogs | §8.8 | `feat/v1-albums` | Sonnet |

Left for wave 2 (needs wave 1 merged): Takeout sidecars wired into the scan and Takeout
dates (B + A), recompute dates on a source type change, viewer zoom/pan, info panel and
"Show in Explorer" (§8.6 browsing), wiring the filter bar and albums into the main window,
the album filter, multi-select "Add to album", and the 100k bench on Windows.

## File ownership

An agent edits only the files it owns, plus new files it creates. Anything else is read
only. If a change outside your files is truly needed, keep it minimal and list it in your
final report.

| File | Owner | Notes for others |
| --- | --- | --- |
| `imagory/scanner.py`, `imagory/thumbs.py`, `imagory/metadata.py`, `imagory/app.py`, `imagory/sources.py`, `imagory/ui/sources_dialog.py`, `imagory/ui/scan_thread.py` | A | |
| `imagory/takeout.py` | B | A must not change it |
| `imagory/dates.py` | read only for all | Already done and tested; A calls it |
| `imagory/ui/main_window.py`, `imagory/ui/grid.py`, `imagory/ui/photo_grid.py`, `imagory/ui/grid_layout.py`, `imagory/ui/grid_query.py` | C | A may change the thumbnail-path call sites only (see contracts); A may change `summary_text` / `results_text` for new counts |
| `imagory/ui/viewer.py` | C | A may change thumbnail/file path lookups only |
| `imagory/ui/filter_bar.py` (new) | D | |
| `imagory/ui/albums.py` (new) | E | |
| `PRD.md`, `README.md`, `tasks.md`, `CLAUDE.md` | lead | Agents report decisions instead of editing these |

### `imagory/db.py` (shared, all SQL lives here)

`db.py` is split into `# --- name ---` sections. Each agent edits only its own sections and
adds new sections at the end of the file, before `# --- settings ---`.

- **A** owns `sources`, `files (scan)`, `scan runs`, `delete_source`, and may append fields to
  the **end** of `GridPhoto` (for example `content_hash`) and update the two existing
  `grid_photos*` queries to select them.
- **C** owns `# --- browsing ---` apart from the above, and adds `month_counts`.
- **D** adds a new `# --- search and filters ---` section. Does not edit existing functions.
- **E** adds a new `# --- albums ---` section. Does not edit `delete_source`: membership
  removal comes from `ON DELETE CASCADE` on `album_photos.photo_id`.

### Migrations

Numbers are fixed in advance so the branches merge cleanly. Append to `MIGRATIONS` in order.

- **Migration 2: E** (albums tables).
- **Migration 3: A** (duplicates, dates, and anything else A needs; one migration only).
- On E's branch `MIGRATIONS` ends at 2. On A's branch, add a placeholder
  `_migration_2` with the docstring "Reserved: albums (merged from feat/v1-albums)" whose
  body is empty, so that A's migration really is number 3. The lead replaces the placeholder
  with E's migration at merge time.

### Tests

Put new tests in new files so branches do not collide: `tests/test_duplicates.py`,
`tests/test_moves.py` (A), `tests/test_sidecars.py` (B), `tests/test_grid_integration.py` (C),
`tests/test_filters.py` (D), `tests/test_albums.py` (E). Change existing test files only
where your own change breaks them, and say so in your report.

## Contracts between agents

- **Thumbnail lookup (A to C).** Today the grid and viewer find thumbnails with
  `thumbs.ensure_thumbnail(source_root, thumbs_root, relative_path)` and `thumb_path`. A
  moves the key to the content hash. A updates the existing call sites in `ui/grid.py` and
  `ui/viewer.py` and keeps one public helper in `thumbs.py` that takes what a `GridPhoto`
  has. C's new thumbnail provider calls the thumbnail lookup through **one** small function
  in its own module, so the lead can retarget it at merge time.
- **Grid rows (A, C, D).** `db.GridPhoto` stays a `NamedTuple`; only A adds fields, at the
  end. D's filtered queries return `GridPhoto` rows with the same columns as
  `db.grid_photos` (copy the select list, and expect the lead to sync it after A merges).
- **Month counts (C, D).** C adds `db.month_counts(conn) -> list[tuple[str | None, int]]`:
  `("YYYY-MM", count)` newest first, then `(None, count)` for "Unknown date" when non-zero,
  over the same rows as `grid_photos`. D adds `db.filtered_month_counts(conn, f)` with the
  same shape. C's grid data source takes its rows and counts from injected callables, so
  the lead can switch it to D's filtered versions.
- **Filter (D).** `db.PhotoFilter` is a frozen dataclass; `PhotoFilter()` means "no filter"
  and must give exactly the same rows as `db.grid_photos`. Fields: `text`, `date_from`,
  `date_to` (inclusive local dates `YYYY-MM-DD`), `source_ids`, `folder` (source id plus
  relative folder prefix), `has_duplicates`, `missing`. The album filter waits for wave 2.
- **Missing photos (A, D).** A photo is missing only when all its files are missing
  (§8.2). D implements the `missing` filter with that rule via `EXISTS` over `files`, so it
  is right whether or not A has merged.
- **Albums (E).** The UI layer is self-contained: dialogs take a connection (or callables)
  and photo ids. E does not edit `main_window.py`; it lists the wiring needed in its report.

## Rules for every agent

1. Read `CLAUDE.md`, the PRD sections for your task, and this file first.
2. Work in your worktree. First `git checkout -b <your branch>` (from the `main` commit the
   worktree starts at), then `uv sync`.
3. Follow `CLAUDE.md`: never write to source folders, no network, SQL only in `db.py`, one
   bad image never stops a scan, `IMAGORY_LIBRARY` set in tests and runs, fixtures generated
   in code. Scan integration tests also assert the source folder is unchanged.
4. Before finishing: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`
   all pass on your branch.
5. Commit in focused Conventional Commits (`feat(db): ...`), each message ending with the
   `Co-Authored-By` line given in your instructions. Do not push, do not merge, do not edit
   `PRD.md`, `README.md`, `tasks.md`, or `CLAUDE.md`.
6. Final report: branch name and commits, what was built, test results, decisions the PRD
   should record (§16), any change outside your files, and wiring notes for wave 2.

## Merge order (lead)

1. B (new code only), 2. E (migration 2), 3. D, 4. A (migration 3, replacing the
placeholder), 5. C (re-targets the thumbnail provider to A's helper and the data source to
D's queries if trivial). Full tests and ruff after each merge; then PRD, README, and
`tasks.md` updates in one docs commit.

## Status

| Agent | Status |
| --- | --- |
| A | started |
| B | started |
| C | started |
| D | started |
| E | merged (296390e) |
