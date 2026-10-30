# Imagory: Guidelines for Claude

Imagory is an offline Windows desktop app (Python 3.12, PySide6, SQLite) for browsing local photo folders and Google Takeout exports. `PRD.md` is the source of truth for scope, architecture, and the data model. Read the relevant PRD section before designing or changing anything. If a request conflicts with the PRD, say so instead of quietly changing course.

## Current focus: V0 only
- Build only what PRD §7 describes: pick a folder, scan it in the background, browse thumbnails, view a photo, reopen from the database.
- V0 still uses the **V1 core schema** (`sources`, `files`, `photos`, `scan_runs`, `settings`) with one source, one photo per file, and empty hash columns (PRD §7.1, §9).
- Out of scope until asked (PRD §7.4): hashing, duplicates, moved-file detection, Takeout parsing, filename dates, albums, search/filters, multiple sources, month headers, the custom virtualized grid, viewer zoom/pan, a settings screen.
- "Done" for V0 is the checklist in PRD §7.5. Refer to it when deciding what to work on next.

## Non-negotiable principles (PRD §3)
- **Never modify the user's photos.** Only read photo files. Never move, rename, edit, or delete them, and never write anything into photo folders. Any code path that opens a source file for writing is a bug.
- **No network access.** No telemetry, update checks, or remote calls. Don't add dependencies that phone home.
- **Thumbnails are disposable** and must regenerate on demand. The database is the durable store.
- **Hide, never destroy.** Database rows are removed only through an explicit, confirmed user action, never automatically.

## Project structure
Planned layout. Nothing exists yet, so create modules as V0 needs them.
- `imagory/` is the application package, one module per job (PRD §6.1):
  - `db.py`: connection setup (WAL, `foreign_keys=ON`, `synchronous=NORMAL`), migrations, **all SQL**
  - `scanner.py`: walk, diff, queue work, apply results
  - `metadata.py`: dimensions, format, EXIF date and orientation
  - `thumbs.py`: generating and locating cached thumbnails
  - `takeout.py`: V1 only, don't create it yet
  - `ui/`: main window, grid, viewer, dialogs
  - `paths.py` (or similar): library folder resolution and the bundled-resource helper (`sys._MEIPASS`)
  - `__main__.py`: entry point. Calls `multiprocessing.freeze_support()` first.
- `tests/`: pytest unit and integration tests. Fixtures are generated in code, not committed image files.
- `scripts/`: dev tools such as the 100,000-photo fake library generator (PRD §10).
- `pyproject.toml` + committed `uv.lock`. `LICENSE` (MIT) at the root.

## Architecture rules
- **Threads (PRD §6.2):** the main thread runs Qt UI only. One scan coordinator `QThread` is the **only database writer** during a scan, committing about 200 files per transaction. Worker processes (`ProcessPoolExecutor`, CPU count minus one) read metadata and make thumbnails in one pass, return plain picklable results, and **never touch the database**. Thumbnails for the grid load through a `QThreadPool` with a size-limited memory cache.
- The UI never touches files or the database directly during a scan. Keep SQL in `db.py`.
- **Scan pipeline (PRD §6.3):** walk (`os.scandir`, no file opens), diff against `files`, process new and changed files, apply, mark missing, record `scan_runs`. Cancel and resume need no saved state, so keep it that way.
- **Walk rules (PRD §7.2):** don't follow symlinks or junctions. Skip dirs starting with `.` or `$` and `System Volume Information`, and skip the library folder if it sits inside the source. Extensions are matched case-insensitively: `.jpg .jpeg .png .gif .heic .heif`.
- One bad image must never stop a scan. Log it, count it as an error, and move on.

## Data conventions (PRD §9)
- Paths are relative to the source root, use `/` separators, keep on-disk case, and use `COLLATE NOCASE`.
- `modified_time` is integer nanoseconds (`st_mtime_ns`).
- `taken_at_local` is ISO 8601 with no offset (`2019-01-31T23:59:59`). UTC timestamps are ISO 8601 ending in `Z`.
- Schema version lives in `PRAGMA user_version`. Each version step is a numbered migration that runs in a transaction, and `imagory.db` is copied to `imagory.db.bak` before migrating. Never edit a released migration; add a new one.
- V0 thumbnails go in `thumbs/ab/cd/<blake2b(relative_path)>.jpg`, 320 px longest edge, JPEG quality 85, EXIF orientation applied, Pillow draft mode for JPEGs.

## Library folder
- Windows uses `%LOCALAPPDATA%\Imagory\`, other platforms `$XDG_DATA_HOME/imagory` (default `~/.local/share/imagory`).
- `IMAGORY_LIBRARY` overrides it. **Always set it in tests and dev runs** so the real library is never touched.
- Never write data next to the executable.

## Development environment
- Development happens on Linux (WSL); the target is Windows 10/11. Avoid Windows-only code where it costs nothing, and keep Windows behaviour (case-insensitive paths, junctions, `%LOCALAPPDATA%`) correct.
- PyInstaller `--onefile` builds and the `.exe` checks in PRD §7.5 must run on real Windows, not WSL. Say so instead of claiming a Windows build was verified.

## Commands
- `uv sync`: install dependencies into `.venv`
- `uv run python -m imagory`: run the app (set `IMAGORY_LIBRARY` to a scratch folder)
- `uv run pytest -q`: run tests
- `uv run ruff check .` and `uv run ruff format --check .`: lint and format check. `uv run ruff format .` applies formatting.
- Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`). Don't hand-edit `uv.lock`.

## Coding style
- Python 3.12, PEP 8, 4-space indent, formatted with ruff. Config lives in `pyproject.toml`.
- `snake_case` for modules, functions, and variables, `PascalCase` for classes and Qt widgets.
- Use type hints on public functions. Prefer small pure functions (walk filtering, diffing, date fallback) so they're unit-testable without Qt.
- Use `pathlib` for filesystem paths and the `logging` module (rotating file in `logs/`) instead of `print`.
- Only add dependencies with permissive or LGPL licences (the app is MIT). Don't copy code from GPL projects such as digiKam.

## Testing
- Use pytest. Write unit tests for walk rules, the scan diff, the date fallback, and migrations. Integration tests scan a generated fixture folder into a temp library (`tmp_path` + `IMAGORY_LIBRARY`).
- Every scan integration test should also assert that the **source folder is unchanged**: same files, sizes, and mtimes, and no new files.
- Real photos are never committed to the repo.
- After a change, run the relevant tests and ruff. Report failures with their output instead of glossing over them.

## Commits and pull requests
- Use Conventional Commits: `type(scope): summary`. Types are `feat`, `fix`, `refactor`, `test`, `docs`, `build`, `chore`. Scopes are module names (`db`, `scanner`, `metadata`, `thumbs`, `ui`) or `prd`. Example: `feat(scanner): add incremental scan diff`.
- Keep commits focused, and only commit when asked.
- PRs should include a short summary, test results, and any PRD impact.

## Keeping docs in sync
- If a change alters scope, architecture, the schema, or a decision, update `PRD.md` (including the decisions log in §16) in the same change, or flag the mismatch.
- Update `README.md` when user-facing behaviour, setup, or build steps change. The README must state the offline/privacy promise prominently (PRD §11).
