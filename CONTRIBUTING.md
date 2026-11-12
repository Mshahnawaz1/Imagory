# Contributing to Imagory

Thanks for your interest in Imagory. Bug reports, testing on real photo collections, and code
are all welcome. This page explains how to report problems, set up a development copy, and get
a change merged.

## Reporting a bug

Open a GitHub issue with:

- your Imagory version and your OS (Windows 10 or 11, or the Linux distro and version);
- what you did, what you expected, and what happened;
- the log files, if you can. **Help > Open log folder** opens them. Logs stay on your computer
  until you attach them yourself.

Please **don't attach your own photos** or Takeout files to an issue. If a particular file
causes the problem, describe it instead: format, camera or phone, size, and file name pattern.

Feedback from real Google Takeout exports is especially useful. File name quirks, sidecar JSON
that wasn't matched, and wrong dates all help tune the rules (PRD §18).

## Before you write code

Imagory has a few rules that every change must follow. A pull request that breaks one won't be
merged, however useful it is otherwise.

1. **Never modify the user's photos.** Code may only read photo files. It must never move,
   rename, edit, or delete them, or write anything into a photo folder. The only exception is
   Clean up Takeout, which writes copies into an output folder the user picks.
2. **No network access.** No telemetry, update checks, crash reporting, or remote calls of any
   kind, and no dependencies that make them.
3. **Thumbnails are disposable.** They must regenerate on demand. The database is the durable
   store.
4. **Hide, never destroy.** Database rows are removed only through an explicit action the user
   has confirmed.
5. **Permissive licences only.** The app is MIT. New dependencies must be MIT, BSD, Apache, or
   LGPL, and must not phone home. Don't copy code from GPL projects such as digiKam.

[PRD.md](PRD.md) is the source of truth for scope, architecture, and the data model. For
anything bigger than a bug fix, **open an issue first** to discuss it. Some features (faces,
reading Takeout zips, portable mode, a settings screen) are deliberately out of scope for now
(PRD §3, §14). Open work is listed in [docs/tasks.md](docs/tasks.md).

## Development setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 if you don't have it.
Development works on Windows and on Linux (including WSL). The main target is Windows 10/11.

```sh
git clone https://github.com/Mshahnawaz1/Imagory.git
cd Imagory
uv sync
```

Run the app against a **scratch library**, so your real one is never touched:

```sh
IMAGORY_LIBRARY=.dev-library uv run python -m imagory            # Linux / WSL
```

```powershell
$env:IMAGORY_LIBRARY = ".dev-library"; uv run python -m imagory  # Windows PowerShell
```

On WSL, `scripts/dev_run.sh` also sets up WSLg and generates a small folder of fake photos to
add as a source.

Generate test photos instead of using your own:

```sh
uv run python scripts/make_fake_library.py .fake-library --count 1000
uv run python scripts/make_fake_library.py .fake-library-takeout --takeout --count 500
```

To build the `.exe` or the Linux binary, see [docs/setup.md](docs/setup.md). Windows builds
must be made on real Windows, not WSL.

## Checks

Run these before you open a pull request, and include the results in it.

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .     # `uv run ruff format .` fixes formatting
```

On a headless Linux machine, set `QT_QPA_PLATFORM=offscreen` for the UI tests. The tests set
`IMAGORY_LIBRARY` to a temporary folder themselves.

## Writing code

- **Layout.** `imagory/` has one module per job. All SQL lives in `imagory/db/` (one module per
  area, re-exported from `imagory/db/__init__.py`). Cleanup code lives in `imagory/cleanup/`, and
  the UI in `imagory/ui/`. [docs/architecture.md](docs/architecture.md) has a diagram.
- **Threads.** The main thread runs only the Qt UI. During a scan, a single coordinator thread
  is the only database writer. Worker processes read metadata and make thumbnails, return
  plain picklable results, and never touch the database. The UI doesn't touch files or the
  database directly during a scan (PRD §6.2).
- **Robustness.** One bad image must never stop a scan. Log it, count it as an error, and
  move on.
- **Data conventions** (PRD §9). Paths are stored relative to the source root with `/`
  separators. `modified_time` is integer nanoseconds. Local times are ISO 8601 with no offset,
  and UTC times end in `Z`.
- **Schema changes.** A schema change is a new numbered migration that runs in a transaction.
  Never edit a migration that has been released. Update the data model in PRD.md in the same
  change.
- **Style.** Python 3.12, formatted with ruff (line length 100), type hints on public functions,
  `pathlib` for paths, and `logging` instead of `print`. Prefer small pure functions that can be
  tested without Qt.
- **Words.** Use the terms in [GLOSSARY.md](GLOSSARY.md) (File, Photo, Primary file, Copy, …)
  in code, UI text, and docs.
- **Windows.** Keep Windows behaviour correct even when you develop on Linux: case-insensitive
  paths, junctions, and `%LOCALAPPDATA%`. Avoid Windows-only code where a portable version
  costs nothing.

## Tests

- Write tests with pytest. Generate fixtures in code. **Never commit real photos** or image
  files.
- Integration tests scan a generated folder into a temporary library (`tmp_path`).
- Every scan integration test must also assert that **the source folder is unchanged**: same
  files, sizes, and modified times, and no new files. `tests/fs_snapshot.py` helps with this.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`.
  Types are `feat`, `fix`, `refactor`, `test`, `docs`, `build`, and `chore`. Scopes are module
  names (`db`, `scanner`, `metadata`, `thumbs`, `ui`, `cleanup`, …) or `prd`. Example:
  `fix(scanner): skip junctions on Windows`.
- Keep each commit and pull request focused on one change.
- In the pull request, give a short summary, the test results, any PRD impact, and which OS you
  tested on. Say so if a change still needs checking on real Windows.
- Keep the docs in step with your change. Update `PRD.md` (including the decisions log in §16)
  when you change scope, architecture, the schema, or a decision. Update `README.md` when you
  change user-facing behaviour or setup. Add a new domain term to `GLOSSARY.md`.

AI coding assistants are fine to use. [CLAUDE.md](CLAUDE.md) holds the same rules in a form
written for them. You are still responsible for reading, testing, and understanding what you
submit.

## Licence

By contributing, you agree that your contributions are licensed under the project's
[MIT licence](LICENSE).
