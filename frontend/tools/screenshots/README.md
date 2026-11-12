# Screenshot tools

Makes the app screenshots in `src/assets/screenshots/` from a **synthetic** library
(drawn scenes, fictional people). Never real photos.

```sh
S=/path/to/scratch/shots            # any scratch folder outside the repo
uv run python frontend/tools/screenshots/make_scenes.py $S/data   # ~40 s, ~250 JPEGs
uv run python frontend/tools/screenshots/capture.py $S            # ~20 s
```

Run both from the repo root (they need the app's `.venv`; Pillow is already there).
`make_scenes.py` writes `data/Photos` (a plain folder with a few duplicates) and
`data/Takeout` (a fictional Google Takeout export with JSON sidecars, people, places, an album).
`capture.py` sets `QT_QPA_PLATFORM=offscreen` and `IMAGORY_LIBRARY=$S/library`, so the real
library is never touched, scans the two folders, and grabs each window. The app only reads `data/`.

Scratch paths shown in the Sources dialog and the viewer's info panel are replaced by neutral
`C:\Users\Sam\...` text in the capture only. Delete `$S/library` before re-running.
