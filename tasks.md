# Imagory V0 tasks

Breakdown of PRD §7 into implementable steps. "Done" for V0 is the checklist in PRD §7.5.
Tick each box when the task is implemented, tested, and committed.

## 1. Project scaffolding
- [x] `pyproject.toml` (Python 3.12, PySide6, Pillow, pillow-heif; dev: pytest, ruff, pyinstaller) and committed `uv.lock`
- [x] ruff and pytest config in `pyproject.toml`
- [x] `.gitignore`, MIT `LICENSE`, rename `Readme.md` to `README.md`
- [x] `imagory/` package skeleton and `__main__.py` calling `multiprocessing.freeze_support()` first

## 2. Paths and logging (`imagory/paths.py`, `imagory/logs.py`)
- [x] Library folder resolution: `IMAGORY_LIBRARY`, `%LOCALAPPDATA%\Imagory`, `$XDG_DATA_HOME/imagory`
- [x] Bundled-resource helper (`sys._MEIPASS`)
- [x] Rotating log file in `<library>/logs/`
- [x] Unit tests

## 3. Database (`imagory/db.py`)
- [x] Connection setup: WAL, `foreign_keys=ON`, `synchronous=NORMAL`
- [x] Migration framework on `PRAGMA user_version`, each step in a transaction, `.bak` copy before migrating an existing DB
- [x] Migration 1: V1 core schema (`sources`, `files`, `photos`, `scan_runs`, `settings`) and indexes (PRD §9)
- [ ] Queries: source create/get/delete, file state for diff, apply batch (insert/update file + photo), mark missing, scan_runs, grid listing
- [x] Unit tests: new DB at current version, pragmas, backup, source delete

## 4. Walk and diff (`imagory/scanner.py`, pure functions)
- [ ] `walk_source`: `os.scandir`, no symlinks/junctions, skip `.`/`$`/`System Volume Information`/library folder, case-insensitive extensions, skipped-by-extension counts
- [ ] `diff`: new / changed / unchanged / disappeared (+ reappeared missing files)
- [ ] Unit tests for walk rules and diff

## 5. Metadata and thumbnails (`imagory/metadata.py`, `imagory/thumbs.py`)
- [ ] Read dimensions, format, EXIF `DateTimeOriginal`, orientation; HEIC via pillow-heif
- [ ] Date fallback: EXIF, else mtime converted to local time; `date_source` `exif`/`mtime`
- [ ] Thumbnail: 320 px, JPEG q85, EXIF transpose, draft mode for JPEG, first GIF frame, `thumbs/ab/cd/<blake2b(relpath)>.jpg`
- [ ] Single-pass worker function returning a picklable result; errors returned, never raised
- [ ] Unit tests (date fallback, orientation, thumbnail path/size, corrupt file)

## 6. Scan coordinator (`imagory/scanner.py`)
- [ ] `Scanner.run()`: walk, diff, process in `ProcessPoolExecutor` (CPU-1), apply in ~200-file transactions, mark missing, record `scan_runs`
- [ ] Progress callback and cooperative cancel (stop queueing, commit in-flight results)
- [ ] Integration tests for PRD §7.5 items 2–5 (rows, no-op rescan, 1 updated + 1 missing, cancel/resume, corrupt image, source unchanged)

## 7. UI (`imagory/ui/`)
- [ ] Main window: empty state with "Choose folder", toolbar (Rescan, Change folder), Help menu ("Open log folder", About with MIT licence)
- [ ] Scan `QThread` wrapping the coordinator, progress bar + Cancel, summary dialog
- [ ] Grid: `QListView` icon mode, model from DB (newest first, ties by path), lazy thumbnail loading via `QThreadPool`, size-limited cache, on-demand regeneration
- [ ] Viewer: fit to window, EXIF orientation, Left/Right/Esc, status line
- [ ] Change folder: confirm, delete source rows and thumbnails, scan new folder
- [ ] Later launches show grid from DB without scanning
- [ ] Offscreen smoke test

## 8. Performance (PRD §7.5 item 6, §10)
- [ ] `scripts/make_fake_library.py`: 100,000 small images with varied EXIF dates and a share of exact duplicates
- [ ] `scripts/bench.py`: full scan, unchanged rescan, time-to-first-screen
- [ ] Run on 100k and record results in `docs/v0-results.md`

## 9. Packaging (PRD §7.5 item 7, §13)
- [ ] PyInstaller `--onefile` spec (`imagory.spec`) and build instructions
- [ ] Windows 10/11 verification and .exe size: **must be done on real Windows** (cannot be verified from WSL)

## 10. Docs
- [ ] README: offline/privacy promise, setup, run, test, build, SmartScreen note
- [ ] PRD sync for any decisions made during implementation
