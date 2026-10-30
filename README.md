# Imagory

A free, open source photo browser for Windows. Point it at a folder of photos and get a
fast, browsable library.

> **Your photos stay yours.** Imagory is fully offline. It makes no network connections:
> no telemetry, no accounts, no update checks. It only *reads* your photos. It never
> changes, moves, renames, or deletes them, and never writes anything into your photo
> folders. Everything it stores (database, thumbnails, logs) stays on your computer in
> its own library folder.

**Status: V0.** Pick a folder, scan it in the background, browse thumbnails, view photos,
and reopen the library later without rescanning. Duplicates, Google Takeout import,
albums, and search are planned for V1 (see [PRD.md](PRD.md)).

## Using Imagory

1. Start Imagory and click **Choose folder…**. Imagory scans the folder and its subfolders
   in the background, and photos appear in the grid as they are processed. You can cancel
   at any time, and **Rescan** picks up where it stopped.
2. Double-click a thumbnail (or press Enter) to open the viewer. **Left** and **Right** move
   between photos. **Back to photos** (or Esc, Backspace, or the mouse back button) returns
   to the grid.
3. **Rescan** finds new, changed, and missing photos. Unchanged photos are skipped, so it is
   quick. Missing photos are hidden, not forgotten. They come back if the file returns.
4. **Change folder…** switches to a different folder. Imagory forgets the old folder's
   library entries and thumbnails. The photos themselves are not touched.

Supported formats: JPEG, PNG, GIF (first frame), and HEIC/HEIF. Photos are sorted newest
first by the date the photo was taken (EXIF), falling back to the file's modified time.

### Where Imagory keeps its data

The library folder is `%LOCALAPPDATA%\Imagory\` and contains `imagory.db` (the database),
`thumbs\` (a thumbnail cache you can delete at any time), and `logs\`. **Help > Open log
folder** opens the logs, for example to attach them to a GitHub issue. Logs are never sent
anywhere.

### Windows SmartScreen and antivirus warnings

Imagory releases are not code-signed, so Windows SmartScreen may say "Windows protected
your PC". Click **More info**, then **Run anyway**. Some antivirus tools also flag unsigned
single-file programs built with PyInstaller. If you'd rather not trust the download, you can
build Imagory yourself from source (below).

## Development

Requires [uv](https://docs.astral.sh/uv/). Development happens on Linux (WSL) or Windows;
the target is Windows 10/11.

```sh
uv sync                                    # install dependencies into .venv
IMAGORY_LIBRARY=.dev-library uv run python -m imagory   # run with a scratch library
uv run pytest -q                           # tests
uv run ruff check . && uv run ruff format --check .
```

Always set `IMAGORY_LIBRARY` to a scratch folder in development, so your real library is
never touched. Tests set it automatically.

### Performance testing

```sh
uv run python scripts/make_fake_library.py .fake-library --count 100000
IMAGORY_LIBRARY=.bench-library uv run python scripts/bench.py .fake-library
```

Results are recorded in [docs/v0-results.md](docs/v0-results.md).

### Building the Windows executable

Run on Windows (not WSL):

```sh
uv sync
uv run python scripts/collect_licenses.py   # refresh THIRD_PARTY_LICENSES.txt
uv run pyinstaller --noconfirm --clean imagory.spec
```

The result is `dist\Imagory.exe`, a single file.

## Licence

Imagory is released under the [MIT licence](LICENSE). Bundled third-party components keep
their own licences, listed in [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
PySide6/Qt is used under the LGPL v3. Note: the pillow-heif binary wheels (HEIC support)
bundle the GPLv2 x265 library. This is an open question to resolve before the first public
release (PRD §18).
