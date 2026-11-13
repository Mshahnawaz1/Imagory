# Contributing to Imagory

Bug reports, testing on real photo collections, and code are all welcome.

## Reporting a bug

Open a [GitHub issue](https://github.com/Mshahnawaz1/Imagory/issues) with:

- your Imagory version and OS (Windows 10/11, or Linux distro and version);
- what you did, what you expected, and what happened;
- the logs if you can (**Help > Open log folder**). They never leave your computer unless you
  attach them.

**Don't attach your own photos or Takeout files.** Describe the file instead: format, camera
or phone, size, and file name pattern. Reports from real Takeout exports (name quirks,
unmatched sidecars, wrong dates) are especially useful.

## Ground rules

A pull request that breaks one of these won't be merged.

1. **Never modify the user's photos.** Only read them. Never move, rename, edit, or delete
   them, or write into a photo folder. (Clean up Takeout writes only to the output folder the
   user picks.)
2. **No network access.** No telemetry, update checks, crash reporting, or dependencies that
   phone home.
3. **Thumbnails are disposable.** They regenerate on demand; the database is the durable store.
4. **Hide, never destroy.** Database rows are removed only by an action the user confirmed.
5. **Permissive licences only.** The app is MIT. Dependencies must be MIT, BSD, Apache, or
   LGPL. Don't copy code from GPL projects such as digiKam.

[PRD.md](PRD.md) is the source of truth for scope and design. For anything bigger than a bug
fix, **open an issue first**. Face recognition, Takeout zips, portable mode, and a settings
screen are out of scope for now (PRD §3, §14). Open work is in [docs/tasks.md](docs/tasks.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12). Windows and Linux,
including WSL, both work; Windows 10/11 is the main target.

```sh
git clone https://github.com/Mshahnawaz1/Imagory.git
cd Imagory
uv sync
```

Always run against a scratch library so your real one is never touched:

```sh
scripts/dev_run.sh                                                     # Linux / WSL
$env:IMAGORY_LIBRARY = "$PWD\.dev-library"; uv run python -m imagory   # Windows PowerShell
```

The `.dev-library` folder is git-ignored and kept between runs; delete it to start fresh. To
test without your own photos, generate fake ones and add that folder in the app:

```sh
uv run python scripts/make_fake_library.py .fake-library --count 1000
uv run python scripts/make_fake_library.py .fake-library-takeout --takeout --count 500
```

Building the `.exe` or the Linux binary is covered in [docs/setup.md](docs/setup.md). Windows
builds must be made on real Windows, not WSL.

## Checks

Run these before opening a pull request and include the results:

```sh
uv run pytest -q                 # or scripts/test.sh unit|integration|ui
uv run ruff check .
uv run ruff format --check .     # `uv run ruff format .` fixes formatting
```

On headless Linux, set `QT_QPA_PLATFORM=offscreen` for the UI tests. Tests set
`IMAGORY_LIBRARY` themselves.

### Benchmarks

If a change could affect speed, run the relevant benchmark and quote the numbers:

```sh
uv run python scripts/make_fake_library.py .fake-library --count 100000
IMAGORY_LIBRARY=.bench-library uv run python scripts/bench.py .fake-library   # scan
uv run python scripts/bench_cleanup.py /path/to/scratch --count 50000         # Takeout cleanup
uv run python scripts/bench_filters.py 100000                                 # search and filters
uv run python scripts/grid_prototype.py --bench                               # grid scrolling
```

Earlier results are in [docs/archive/v0-results.md](docs/archive/v0-results.md).

## Writing code

- **Layout:** one module per job in `imagory/`. All SQL is in `imagory/db/`, cleanup in
  `imagory/cleanup/`, UI in `imagory/ui/`. See [docs/architecture.md](docs/architecture.md).
- **Threads:** the main thread runs only the Qt UI. During a scan, one coordinator thread is
  the only database writer; worker processes return plain picklable results and never touch
  the database (PRD §6.2).
- **Robustness:** one bad image must never stop a scan. Log it, count it, move on.
- **Data** (PRD §9): paths relative to the source root with `/` separators, `modified_time` in
  integer nanoseconds, local times ISO 8601 without offset, UTC times ending in `Z`.
- **Schema changes:** add a new numbered migration (never edit a released one) and update the
  data model in PRD.md.
- **Style:** Python 3.12, ruff (line length 100), type hints on public functions, `pathlib`,
  `logging` instead of `print`, small pure functions that test without Qt.
- **Terms:** use the words in [GLOSSARY.md](GLOSSARY.md) in code, UI text, and docs.
- **Windows:** keep case-insensitive paths, junctions, and `%LOCALAPPDATA%` correct even when
  developing on Linux.

## Tests

- pytest, with fixtures generated in code. **Never commit real photos** or image files.
- Integration tests scan a generated folder into a temporary library (`tmp_path`).
- Every scan integration test also asserts **the source folder is unchanged** (same files,
  sizes, and mtimes, nothing new). `tests/fs_snapshot.py` helps.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, for
  example `fix(scanner): skip junctions on Windows`. Types: `feat`, `fix`, `refactor`, `test`,
  `docs`, `build`, `chore`. Scopes: module names (`db`, `scanner`, `ui`, `cleanup`, …) or `prd`.
- Keep each commit and pull request to one change.
- In the pull request, give a summary, test results, any PRD impact, and the OS you tested on.
  Say if it still needs checking on real Windows.
- Keep docs in step: `PRD.md` (and its §16 decisions log) for scope, architecture, schema, or
  decisions; `README.md` for user-facing behaviour or setup; `GLOSSARY.md` for new terms.

AI coding assistants are fine; [CLAUDE.md](CLAUDE.md) has the same rules written for them. You
are still responsible for understanding and testing what you submit.

## Licence

By contributing, you agree that your contributions are licensed under the
[MIT licence](LICENSE).
