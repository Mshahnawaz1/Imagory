"""Takeout cleanup in the main window: actions, the wizard through a full run, Add to library,
plain Add source of an output, and the swap offer (offscreen Qt)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from imagory import db, paths, sources  # noqa: E402
from imagory.ui import cleanup_wizard as cw  # noqa: E402
from imagory.ui import main_window as mw  # noqa: E402
from tests.fs_snapshot import assert_unchanged, snapshot  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_fake_library.py"
ALBUM_TITLE = "Lisbon trip 2004"
WAIT = 120_000


@pytest.fixture(scope="module")
def takeout(tmp_path_factory):
    """A small fake Takeout with the cleanup cases (read only for every test)."""
    base = tmp_path_factory.mktemp("fake")
    subprocess.run(
        [sys.executable, str(SCRIPT), str(base), "--takeout", "--cleanup", "--count", "12"]
        + ["--duplicates", "0.2", "--seed", "3"],
        check=True,
        capture_output=True,
    )
    return base / "Takeout"


@pytest.fixture
def dialogs(monkeypatch):
    """Record message boxes; ``answers['question']`` is what the user clicks."""
    seen: dict[str, list] = {"information": [], "warning": [], "question": []}
    answers = {"question": QMessageBox.StandardButton.Yes}

    def recorder(kind):
        def fake(parent, title, text, *a, **k):
            seen[kind].append((title, text))
            return answers.get(kind)

        return fake

    for kind in seen:
        monkeypatch.setattr(QMessageBox, kind, staticmethod(recorder(kind)))
    return seen, answers


@pytest.fixture
def window(qtbot, tmp_path, dialogs):
    root = paths.ensure_library_dir()
    conn = db.open_database(paths.db_path(root))
    win = mw.MainWindow(root, conn)
    qtbot.addWidget(win)
    win.resize(900, 700)
    win.show()
    win.cleanup.options = {"workers": 2}
    yield win
    if win.cleanup.wizard is not None:
        win.cleanup.wizard.close()
    win.close()
    conn.close()


def run_wizard(win, qtbot, inp: Path, out: Path):
    """Drive the wizard from the input page to the done page."""
    win.cleanup.open_wizard()
    wiz = win.cleanup.wizard
    wiz.set_input(inp)
    wiz.set_output(out)
    wiz.output_next.click()
    qtbot.waitUntil(lambda: wiz.page == cw.PAGE_PREVIEW, timeout=WAIT)
    wiz.preview_start.click()
    qtbot.waitUntil(lambda: wiz.page == cw.PAGE_DONE or wiz._failed, timeout=WAIT)
    assert wiz.page == cw.PAGE_DONE, wiz.copy_status.text()
    qtbot.waitUntil(lambda: not win.cleaning, timeout=WAIT)
    return wiz


def wait_scan(win, qtbot):
    qtbot.waitUntil(lambda: not win.scanning, timeout=WAIT)
    qtbot.waitUntil(lambda: win.grid_source.complete, timeout=WAIT)


def test_actions_follow_scan_and_cleanup(window, takeout, qtbot, tmp_path):
    win = window
    assert win.cleanup_action.isEnabled()
    assert win.cleanup_action in win.toolbar.actions()
    file_menu = next(a.menu() for a in win.menuBar().actions() if a.text() == "&File")
    assert win.cleanup_action in file_menu.actions()

    before = snapshot(takeout)
    win.source_actions.add_source(takeout)  # a scan: cleanup is disabled until it ends
    assert win.scanning
    assert not win.cleanup_action.isEnabled()
    wait_scan(win, qtbot)
    assert win.cleanup_action.isEnabled()
    assert win.add_action.isEnabled()

    # During a cleanup: scanning, Add source, sources and album actions are disabled.
    win.cleanup.open_wizard()
    wiz = win.cleanup.wizard
    wiz.set_input(takeout)
    wiz.set_output(tmp_path / "out")
    wiz.output_next.click()
    assert win.cleaning
    assert not win.add_action.isEnabled()
    assert not win.rescan_action.isEnabled()
    assert not win.manage_albums_action.isEnabled()
    win.scan.rescan()
    assert not win.scanning
    qtbot.waitUntil(lambda: wiz.page == cw.PAGE_PREVIEW, timeout=WAIT)
    qtbot.waitUntil(lambda: not win.cleaning, timeout=WAIT)
    assert win.add_action.isEnabled()
    assert win.rescan_action.isEnabled()
    assert_unchanged(takeout, before)


def test_second_wizard_raises_the_first(window, qtbot):
    window.cleanup.open_wizard()
    first = window.cleanup.wizard
    assert first is not None and first.isVisible()
    window.cleanup.open_wizard()
    assert window.cleanup.wizard is first


def test_full_run_and_add_to_library(window, takeout, qtbot, tmp_path, dialogs):
    win = window
    out = tmp_path / "out"
    before = snapshot(takeout)
    wiz = run_wizard(win, qtbot, takeout, out)
    assert wiz.done_summary.text().startswith("All ")  # copied and verified
    assert paths.cleanup_db_path(out).is_file()
    assert win.cleanup._run_id is not None
    assert not wiz.swap_button.isVisible()  # the input isn't a library source

    wiz.add_button.click()
    assert win.scanning
    wait_scan(win, qtbot)
    cleaned = [s for s in win.sources if s.type == "cleaned"]
    assert len(cleaned) == 1
    assert len(win.photos) > 0
    assert db.list_people(win.conn)  # people came from the output database
    assert ALBUM_TITLE in [a.name for a in db.list_albums(win.conn)]
    assert not dialogs[0]["question"]  # no old source, so no swap offer
    assert_unchanged(takeout, before)


def test_plain_add_source_of_output_imports_albums(window, takeout, qtbot, tmp_path):
    win = window
    out = tmp_path / "out"
    before = snapshot(takeout)
    wiz = run_wizard(win, qtbot, takeout, out)
    wiz.close()
    assert win.cleanup.wizard is None or not win.cleanup.wizard.isVisible()

    assert sources.detect_source_type(out).type == "cleaned"
    source = win.source_actions.add_source(out)  # the Add source path, type detected
    assert source is not None and source.type == "cleaned"
    wait_scan(win, qtbot)
    assert ALBUM_TITLE in [a.name for a in db.list_albums(win.conn)]
    assert len(win.photos) > 0
    assert_unchanged(takeout, before)


def test_add_source_of_a_non_output_cleaned_is_refused(window, tmp_path, dialogs):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert window.cleanup.add_cleaned_output(plain, None, None) is None
    assert dialogs[0]["warning"]
    assert window.sources == []


def test_swap_refused_before_scan_then_works(window, takeout, qtbot, tmp_path, dialogs):
    win = window
    seen, answers = dialogs
    out = tmp_path / "out"
    before = snapshot(takeout)
    win.source_actions.add_source(takeout)
    wait_scan(win, qtbot)
    old = sources.source_for_folder(win.conn, takeout)
    assert old is not None and old.type != "cleaned"

    wiz = run_wizard(win, qtbot, takeout, out)
    assert wiz._done.input_is_source
    answers["question"] = QMessageBox.StandardButton.No  # decline the automatic offer
    wiz.add_button.click()
    assert wiz.swap_button.isVisible()
    assert win.scanning

    wiz.swap_button.click()  # too early: the scan is still running
    assert any("Wait" in text for _, text in seen["information"])
    assert sources.source_for_folder(win.conn, takeout) is not None

    wait_scan(win, qtbot)
    assert len(seen["question"]) == 1  # the offer after the scan, declined
    assert "no longer be listed" in seen["question"][0][1] or "Replace" in seen["question"][0][0]
    assert sources.source_for_folder(win.conn, takeout) is not None

    answers["question"] = QMessageBox.StandardButton.Yes
    wiz.swap_button.click()
    assert sources.source_for_folder(win.conn, takeout) is None
    assert [s.type for s in win.sources] == ["cleaned"]
    assert len(win.photos) > 0
    assert ALBUM_TITLE in [a.name for a in db.list_albums(win.conn)]
    assert_unchanged(takeout, before)


def test_cancel_returns_to_output_page(window, takeout, qtbot, tmp_path):
    win = window
    before = snapshot(takeout)
    win.cleanup.open_wizard()
    wiz = win.cleanup.wizard
    wiz.set_input(takeout)
    wiz.set_output(tmp_path / "out")
    wiz.output_next.click()
    wiz.cancel_requested.emit()
    qtbot.waitUntil(lambda: not win.cleaning, timeout=WAIT)
    # Either the cancel won the race or the analysis had already finished.
    assert wiz.page in (cw.PAGE_OUTPUT, cw.PAGE_PREVIEW)
    assert_unchanged(takeout, before)


def test_cancel_clears_a_queued_job(window, takeout, qtbot, tmp_path):
    win = window
    win.cleanup.open_wizard()
    wiz = win.cleanup.wizard
    win.cleanup._queued = ("copy", takeout, tmp_path / "out", wiz)
    wiz.cancel_requested.emit()
    assert win.cleanup._queued is None


def test_result_of_a_closed_wizards_job_skips_the_new_wizard(window, takeout, qtbot, tmp_path):
    win = window
    before = snapshot(takeout)
    win.cleanup.open_wizard()
    first = win.cleanup.wizard
    first.set_input(takeout)
    first.set_output(tmp_path / "out")
    first.output_next.click()
    assert win.cleaning
    first.close()  # the dialog answers yes: the window goes, the job is cancelled
    qtbot.waitUntil(lambda: win.cleanup.wizard is None, timeout=WAIT)
    win.cleanup.open_wizard()
    second = win.cleanup.wizard
    assert second is not first
    qtbot.waitUntil(lambda: not win.cleaning, timeout=WAIT)
    assert second.page == cw.PAGE_INPUT  # nothing from the first job reached it
    assert win.cleanup._run_id is None
    assert_unchanged(takeout, before)


def test_albums_arrive_after_a_cancelled_scan_and_a_rescan(window, takeout, qtbot, tmp_path):
    win = window
    before = snapshot(takeout)
    wiz = run_wizard(win, qtbot, takeout, tmp_path / "out")
    wiz.add_button.click()
    win.scan.cancel()
    wait_scan(win, qtbot)
    win.scan.rescan()
    if win.scanning:
        wait_scan(win, qtbot)
    assert ALBUM_TITLE in [a.name for a in db.list_albums(win.conn)]
    assert_unchanged(takeout, before)


def test_refused_add_hides_the_swap_button(window, takeout, qtbot, tmp_path, dialogs):
    win = window
    before = snapshot(takeout)
    wiz = run_wizard(win, qtbot, takeout, tmp_path / "out")
    wiz.swap_button.show()
    wiz.swap_label.show()
    win.source_actions.add_source(takeout)  # a scan is running, so Add to library is refused
    assert win.scanning
    wiz.add_button.click()
    assert wiz.add_button.isEnabled()
    assert not wiz.swap_button.isVisible()
    assert win.cleanup._swap_context is None
    assert not [s for s in win.sources if s.type == "cleaned"]
    wait_scan(win, qtbot)
    assert_unchanged(takeout, before)
