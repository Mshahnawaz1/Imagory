# Imagory: Guidelines for Claude

Imagory is an offline Windows desktop app (Python 3.12, PySide6, SQLite) for browsing local photo folders and Google Takeout exports. `PRD.md` is the source of truth for scope, architecture, and the data model. Read the relevant PRD section before designing or changing anything. If a request conflicts with the PRD, say so instead of quietly changing course.

## Current focus: V1
- V0 (PRD §7) is implemented; its Windows `.exe` checks in §7.5 are still pending.
- V1 is PRD §8, broken into tasks in `docs/tasks.md`. Work section by section and tick a box only once the item is implemented, tested, and committed.
- Still out of scope (PRD §3, §14): faces, Takeout zips (V1.5), portable mode, a settings screen.

## Non-negotiable principles (PRD §3)
- **Never modify the user's photos.** Only read photo files. Never move, rename, edit, or delete them, and never write anything into photo folders. Any code path that opens a source file for writing is a bug.
- **No network access.** No telemetry, update checks, or remote calls. Don't add dependencies that phone home.
- **Thumbnails are disposable** and must regenerate on demand. The database is the durable store.
- **Hide, never destroy.** Database rows are removed only through an explicit, confirmed user action, never automatically.

## Project structure
Create new modules as the work needs them.
- `imagory/` is the application package, one module per job (PRD §6.1):
  - `db/`: **all SQL**, one module per area (`connection`, `schema`, `sources`, `files`, `photos`, `grid`, `albums`, `people`, `photo_info`, `cleanup`, `cleaned`, `settings`). `imagory/db/__init__.py` re-exports every public name, so callers write `from imagory import db` and `db.<name>`
  - `scanner.py`: walk, diff, queue work, apply results
  - `metadata.py`: dimensions, format, EXIF date and orientation
  - `thumbs.py`: generating and locating cached thumbnails
  - `sources.py`: source checks, relocation, removal
  - `takeout.py`: Takeout detection, sidecar matching and reading (PRD §8.3)
  - `cleanup/`: Takeout cleanup (PRD §8.10): `types`, `checks`, `inventory`, `planning` (pure rules), `hashing`, `copying`, `export`, and `pipeline` (the stages, run by `ui/cleanup_thread.py`). Its `__init__.py` imports nothing, so `db` can import `cleanup.types` without a cycle
  - `dates.py`: date rules (PRD §8.4), pure functions
  - `ui/`: main window, grid, viewer, dialogs
  - `paths.py` (or similar): library folder resolution and the bundled-resource helper (`sys._MEIPASS`)
  - `__main__.py`: entry point. Calls `multiprocessing.freeze_support()` first.
- `tests/`: pytest unit and integration tests. Fixtures are generated in code, not committed image files.
- `scripts/`: dev tools such as the 100,000-photo fake library generator (PRD §10).
- `pyproject.toml` + committed `uv.lock`. `LICENSE` (MIT) at the root.

## Architecture rules
- **Threads (PRD §6.2):** the main thread runs Qt UI only. One scan coordinator `QThread` is the **only database writer** during a scan, committing about 200 files per transaction. Worker processes (`ProcessPoolExecutor`, CPU count minus one) read metadata and make thumbnails in one pass, return plain picklable results, and **never touch the database**. Thumbnails for the grid load through a `QThreadPool` with a size-limited memory cache.
- The UI never touches files or the database directly during a scan. Keep SQL in `imagory/db/`.
- **Scan pipeline (PRD §6.3):** walk (`os.scandir`, no file opens), diff against `files`, process new and changed files, apply, mark missing, record `scan_runs`. Cancel and resume need no saved state, so keep it that way.
- **Walk rules (PRD §7.2):** don't follow symlinks or junctions. Skip dirs starting with `.` or `$` and `System Volume Information`, and skip the library folder if it sits inside the source. Extensions are matched case-insensitively: `.jpg .jpeg .png .gif .heic .heif`.
- One bad image must never stop a scan. Log it, count it as an error, and move on.

## Data conventions (PRD §9)
- Paths are relative to the source root, use `/` separators, keep on-disk case, and use `COLLATE NOCASE`.
- `modified_time` is integer nanoseconds (`st_mtime_ns`).
- `taken_at_local` is ISO 8601 with no offset (`2019-01-31T23:59:59`). UTC timestamps are ISO 8601 ending in `Z`.
- Schema version lives in `PRAGMA user_version`. A new database is created from one baseline schema (version 5). Each later change is a numbered migration that runs in a transaction, and `imagory.db` is copied to `imagory.db.bak` before migrating. Never edit a released migration; add a new one.
- Thumbnails go in `thumbs/ab/cd/<content_hash>.jpg` (BLAKE2b, 16 bytes, shared across sources), 320 px longest edge, JPEG quality 85, EXIF orientation applied, Pillow draft mode for JPEGs.

## Library folder
- Windows uses `%LOCALAPPDATA%\Imagory\`, other platforms `$XDG_DATA_HOME/imagory` (default `~/.local/share/imagory`).
- `IMAGORY_LIBRARY` overrides it. **Always set it in tests and dev runs** so the real library is never touched.
- Never write data next to the executable.

## Development environment
- Development happens on Linux (WSL); the main target is Windows 10/11, and a best-effort Linux x86_64 binary is also released (PRD §13). Avoid Windows-only code where it costs nothing, and keep Windows behaviour (case-insensitive paths, junctions, `%LOCALAPPDATA%`) correct.
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
