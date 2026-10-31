# Known issues

## 1. Intermittent crash in the UI tests ("pure virtual method called")

**Status:** fixed 2026-10-31 on `feat/v1-sources` (see "Resolution" at the end).
Found the same day while finishing V1 §8.1 Sources.

**How it was fixed:** the grid no longer creates and deletes a `GridQueryThread` (a
`QThread` subclass) for each reload. `GridQuery` in `imagory/ui/grid_query.py` runs each
query as a `QRunnable` on its own one-thread `QThreadPool`, the same way thumbnails load.
Pool threads are reused, so no thread object is deleted. The stress test went from
crashing in 5 of 5 runs to 0 crashes in 750 iterations. The `wait()` calls from
attempt 6 did not help, so they were removed.

### Symptom

- The full test suite sometimes dies with `pure virtual method called`, then
  `terminate called without an active exception` and `Fatal Python error: Aborted`.
  Sometimes it is `Fatal Python error: Segmentation fault` instead.
- "Pure virtual method called" means C++ code called a method on an object that was
  already being destroyed.
- It always happens right after `tests/test_ui.py::test_large_grid_loads_first_page_then_rest`,
  while pytest-qt processes leftover Qt events (`pytestqt/plugin.py`, `_process_events`,
  called from `pytest_runtest_call`).
- Rate: 2 of 16 full-suite runs (seen by the §8.1 agent), 1 of 23 runs in a later loop.

### Why this test

It is the only test that sets `FIRST_PAGE = 5`. That makes the main window start a
`GridQueryThread` (`imagory/ui/grid_query.py`) to load the rest of the grid in the
background. No other test starts that thread.

The thread is cleaned up in `imagory/ui/main_window.py`:

```py
thread.finished.connect(lambda: self._grid_thread_done(thread))


def _grid_thread_done(self, thread):
    self._grid_threads.discard(thread)
    thread.deleteLater()
```

### Fast reproduction

Running only that test 150 times in one process crashes within about 1 to 13 iterations.
The stress module re-imports the test, parametrizes it 150 times, and redefines the
`library` fixture. It lives in the session scratchpad (`stress/test_stress_grid.py` and
`stress/variants.py`) and is not committed:

```sh
QT_QPA_PLATFORM=offscreen uv run python -X faulthandler -m pytest -q -p no:cacheprovider \
    --rootdir . <scratchpad>/stress/test_stress_grid.py
```

`variants.py` monkeypatches `MainWindow` methods, chosen by the `VARIANT` environment
variable, to test cleanup strategies without editing the source.

### What was proved

| Experiment (150 iterations per run) | Result |
|---|---|
| Original code: the lambda on `finished` calls `thread.deleteLater()` | crashed 3 of 3 runs |
| Same lambda, but the thread object is never deleted | 0 of 3 runs crashed (450 iterations) |
| `thread.wait()` then `deleteLater()`, patched in by the test | 0 of 6 runs crashed (900 iterations) |

Deleting the `GridQueryThread` object is what triggers the crash. Qt emits `finished`
slightly before the thread has fully stopped, and deleting the object in that gap is a
known way to get exactly this error.

### What was tried and did not work

1. **The §8.1 agent's loop to find the crashing test with verbose output.** 20 runs of
   `pytest -v` did not reproduce it, because the crash is too rare in the full suite.
   The agent was interrupted while trying again.
2. **Getting all thread stacks with `python -X faulthandler`.** The dump only shows the
   main thread inside Qt's event loop. It does not show which C++ object was destroyed
   or which thread touched it.
3. **Theory: PySide runs lambda slots in the worker thread.** If that were true,
   `deleteLater()` would race with the thread's shutdown. A standalone check
   (`stress/which_thread.py`) showed that both lambda slots and bound-method slots run in
   the main thread on PySide6 6.12.0, so this theory is wrong.
4. **A bound method that finds the thread with `self.sender()`.** `sender()` returned
   `None`, so every run failed with
   `AttributeError: 'NoneType' object has no attribute 'deleteLater'`.
5. **`finished` connected straight to `thread.deleteLater` (a C++ slot).** The variant
   never removed the thread from `_grid_threads`, so `closeEvent` later called `wait()`
   on deleted objects and the tests failed. The variant itself was broken, so it says
   nothing about the crash.
6. **Waiting before deleting, written into the real code.** `main_window.py` now calls
   `thread.wait()` before `thread.deleteLater()` in `_grid_thread_done`, and also calls
   `self.scan_thread.wait()` before `self.scan_thread.deleteLater()` in
   `_on_scan_thread_finished`. **It still crashed in 4 of 5 stress runs**, although the
   patched-in version of the grid-thread part passed 6 of 6. The only difference is the
   extra `scan_thread.wait()`. The experiment to isolate it was stopped before it ran.

### Why it is not solved yet

- The working fix from the experiments did not carry over to the real code, and the
  reason is unknown.
- gdb is not installed, so there is no C++ backtrace.
- The crash is intermittent, so each experiment needs many repeated runs before its
  result means anything.
- It may be older than §8.1. `main` has the same lambda plus `deleteLater()` and the same
  test. The §8.1 agent saw 0 crashes in 14 full-suite runs on `main`, which is too few to
  rule that out. The stress test has not been run against `main`.

### Next steps

1. **Finish the isolation.** Stress the real code with `scan_thread.wait()` removed
   (`VARIANT=nowaitscan` in `variants.py`), then run the stress test against `main` to see
   whether §8.1 introduced the crash.
2. **Install gdb** (`sudo apt install gdb`) and run the stress test under
   `gdb -batch -ex run -ex "thread apply all bt"` to see exactly which object is
   destroyed and by which thread.
3. **Workaround if it needs to be unblocked:** never delete grid-query threads (0 crashes
   in 450 iterations). The cost is one small finished thread object per grid reload of
   more than 500 photos, freed when the window closes.

### Resolution (2026-10-31)

The stress harness was rebuilt in a new session's scratchpad (the old one was gone). It
runs the test 150 times in one process (`N` env var), and a small loop script repeats
whole runs.

1. **Re-check attempt 6 (`wait()` then `deleteLater()` in the real code).** It crashed
   in **5 of 5** runs, each time within 2 to 40 iterations (`Segmentation fault`,
   in `pytestqt` `_process_events`). So `wait()` does not prevent it. The patched-in
   0-of-6 result from before was probably luck or a difference in the variant. gdb is
   still not installed (and `sudo` needs a password), so there is still no C++ backtrace.
2. **Stop creating and deleting a `QThread` per grid reload.** `imagory/ui/grid_query.py`
   now has `GridQuery`, which owns a one-thread `QThreadPool` and runs each query as a
   `QRunnable`. A `QObject` that lives in the main thread carries the result back,
   the same pattern `ThumbnailLoader` uses. Pool threads are reused, so no thread
   object is deleted after a query. A new reload clears any query still queued, and
   `closeEvent` calls `grid_query.wait()`. Result: **0 crashes in 5 of 5 stress runs
   (750 iterations)**, plus 5 clean full-suite runs (96 passed, 1 skipped).

The exact C++ cause (why deleting this finished `QThread` subclass crashes even after
`wait()`) is still unknown, because there is no native backtrace. `ScanThread` is still
deleted with `deleteLater()` on `finished`, as on `main`. It was deleted once in every stress iteration (750 times)
without crashing, so it was left as it is. If it ever shows the same symptom, move it to
the same pattern.
