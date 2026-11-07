# Work log

Short dated entries of what landed and the key facts behind it. Details live in the PRD, the plan
(`docs/takeout-cleanup-plan.md`) and git history.

## 2026-11-07: Takeout cleanup, wave 3

- **Merged:** I wiring (`968aeb9`), J benchmark (`0d55c6c`). `main`: 636 passed, 1 skipped; ruff
  clean; no new hits for the safety grep.
- **I (UI wiring):** Clean up Takeout… in the toolbar and File menu. Each job runs in a
  `CleanupThread` with its own pipeline. Add to library and plain Add source of an output: add
  `cleaned` source, scan, import albums, offer swap (only when `swap_ready`). Review fixes: results
  go only to the wizard that started the job; Cancel drops queued jobs; albums are imported after
  every completed scan of a `cleaned` source; a refused add hides the swap button.
- **J (benchmark):** `scripts/bench_cleanup.py SCRATCH --count N`. The first version used the wrong
  input (it included the generator's `manifest.json`), so those runs don't count.
- **Benchmark, 50,000 photos (the author chose 50k over 100k), WSL2, 8 CPUs, 7 workers, low free
  RAM, not Windows:** analyse photos 114 s, other files 8 s, plan 21 s, copy 162 s (47,288
  copies), finalise 64 s; total about 6 min. 0 failed, 0 unaccounted.

## 2026-11-07: Takeout cleanup, L2 (lead)

- PRD: §8.4 mtime ban limited to Takeout-derived dates; §8.10 lock scope, album import retry,
  swap rule; §10 cleanup benchmark (50k, WSL); seven §16 rows; §18 question 9. README:
  cleanup section, privacy note exception. Plan ticked; Windows checks in `docs/tasks.md`.
- Safety pass on `main`: all writes under the output folder, `.imagory/` or the library.
  Removed the unused `cleanup_copy.remove_stale_parts(folder)` (it deleted every `*.part` under
  a folder); the pipeline removes only its own targets' `.part` files.
