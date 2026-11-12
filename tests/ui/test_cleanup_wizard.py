"""The cleanup wizard shell: page flow, folder messages, cancel prompt, preview and done pages."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imagory.cleanup.types import DoneResult, FolderCheck, PreviewCounts  # noqa: E402
from imagory.ui import cleanup_wizard as cw  # noqa: E402

IN = Path("/takeout")
OUT = Path("/out")
REPORT = Path("/out/.imagory/report.html")


def counts(**kw) -> PreviewCounts:
    base = dict(
        photos=10,
        other_files=2,
        json_files=3,
        duplicates=4,
        duplicate_bytes=3 * 1024**2,
        trash_skipped=1,
        unmatched_sidecars=5,
        albums=6,
        bytes_to_copy=2 * 1024**3,
        free_bytes=10 * 1024**3,
    )
    base.update(kw)
    return PreviewCounts(**base)


def done(**kw) -> DoneResult:
    base = dict(
        copied=7,
        failed=(),
        unaccounted=(),
        safe_to_delete=True,
        report_path=REPORT,
        output_path=OUT,
        input_is_source=False,
    )
    base.update(kw)
    return DoneResult(**base)


@pytest.fixture
def state():
    return {"input": (True, "Google Takeout folder"), "mode": "new", "answer": True}


@pytest.fixture
def wiz(qtbot, state):
    w = cw.CleanupWizard(
        lambda p: state["input"],
        lambda i, o: FolderCheck(state["mode"], f"msg for {o}"),
        confirm_cancel=lambda parent: state["answer"],
    )
    qtbot.addWidget(w)
    w.show()
    return w


def to_analyse(wiz):
    wiz.set_input(IN)
    wiz.input_next.click()
    wiz.set_output(OUT)
    wiz.output_next.click()


def test_page_flow(wiz, qtbot):
    assert wiz.page == cw.PAGE_INPUT
    assert not wiz.input_next.isEnabled()
    wiz.set_input(IN)
    wiz.input_next.click()
    assert wiz.page == cw.PAGE_OUTPUT
    wiz.set_output(OUT)
    with qtbot.waitSignal(wiz.analyse_requested) as sig:
        wiz.output_next.click()
    assert sig.args == [IN, OUT]
    assert wiz.page == cw.PAGE_ANALYSE
    wiz.set_progress("Reading", 3, 10)
    assert wiz.analyse_bar.value() == 3 and "3 of 10" in wiz.analyse_status.text()
    wiz.show_preview(counts())
    assert wiz.page == cw.PAGE_PREVIEW
    with qtbot.waitSignal(wiz.copy_requested):
        wiz.preview_start.click()
    assert wiz.page == cw.PAGE_COPY
    wiz.show_done(done())
    assert wiz.page == cw.PAGE_DONE


def test_input_warning_does_not_block(wiz, state):
    state["input"] = (False, "does not look like Takeout")
    wiz.set_input(IN)
    assert "does not look like Takeout" in wiz.input_message.text()
    assert wiz.input_next.isEnabled()


def test_refused_output_blocks_next(wiz, state):
    state["mode"] = "refused"
    wiz.set_input(IN)
    wiz.set_output(OUT)
    assert wiz.output_message.text() == "msg for /out"
    assert not wiz.output_next.isEnabled()
    state["mode"] = "add"
    wiz.set_output(OUT)
    assert wiz.output_next.isEnabled()


def test_close_asks_to_cancel(wiz, qtbot, state):
    to_analyse(wiz)
    state["answer"] = False
    with qtbot.assertNotEmitted(wiz.cancel_requested):
        wiz.close()
    assert wiz.isVisible()
    state["answer"] = True
    with qtbot.waitSignal(wiz.cancel_requested):
        wiz.close()
    assert not wiz.isVisible()


def test_close_when_idle_does_not_ask(wiz, qtbot, state):
    state["answer"] = False
    with qtbot.assertNotEmitted(wiz.cancel_requested):
        wiz.close()
    assert not wiz.isVisible()


def test_cancel_button_and_error(wiz, qtbot):
    to_analyse(wiz)
    with qtbot.waitSignal(wiz.cancel_requested):
        wiz.analyse_cancel.click()
    wiz.show_error("disk <b>full</b>")
    assert "disk <b>full</b>" in wiz.analyse_status.text()
    assert not wiz.working
    wiz.analyse_cancel.click()
    assert wiz.page == cw.PAGE_OUTPUT


def test_preview_shows_all_fields(wiz):
    wiz.show_preview(counts())
    text = wiz.preview_text.text()
    for expected in ("10", "2", "3", "4", "3.0 MB", "5", "6", "2.0 GB", "10.0 GB"):
        assert expected in text
    assert wiz.preview_start.isEnabled()
    assert wiz.preview_reason.text() == ""


def test_preview_disables_copy_on_low_space(wiz):
    wiz.show_preview(counts(bytes_to_copy=2048, free_bytes=1024))
    assert not wiz.preview_start.isEnabled()
    assert "Not enough free space" in wiz.preview_reason.text()
    wiz.show_preview(counts(bytes_to_copy=1024, free_bytes=1024))
    assert wiz.preview_start.isEnabled()


def test_format_bytes():
    assert cw.format_bytes(5) == "5 B"
    assert cw.format_bytes(2048) == "2.0 KB"
    assert cw.format_bytes(3 * 1024**3) == "3.0 GB"


def test_done_safe_wording(wiz):
    wiz.show_done(done(copied=12))
    assert wiz.done_summary.text() == (
        "All 12 files copied and verified. You can delete the original Takeout folder if you "
        "want; Imagory never does."
    )
    assert wiz.done_summary.textFormat() == cw.Qt.TextFormat.PlainText


def test_done_failures_listed_plain(wiz):
    wiz.show_done(done(safe_to_delete=False, failed=("a/<b>x</b>.jpg",), unaccounted=("c.mov",)))
    assert "copied and verified" not in wiz.done_summary.text()
    details = wiz.done_details.text()
    assert "a/<b>x</b>.jpg" in details and "c.mov" in details
    assert wiz.done_details.textFormat() == cw.Qt.TextFormat.PlainText


def test_done_buttons_emit_paths(wiz, qtbot):
    wiz.show_done(done())
    with qtbot.waitSignal(wiz.open_folder_requested) as f:
        wiz.open_folder_button.click()
    assert f.args == [OUT]
    with qtbot.waitSignal(wiz.open_report_requested) as r:
        wiz.open_report_button.click()
    assert r.args == [REPORT]


def test_swap_offer_only_for_source_inputs(wiz, qtbot):
    wiz.show_done(done())
    with qtbot.waitSignal(wiz.add_to_library_requested):
        wiz.add_button.click()
    assert not wiz.swap_button.isVisible()
    wiz.show_done(done(input_is_source=True))
    assert not wiz.swap_button.isVisible()
    wiz.add_button.click()
    assert wiz.swap_button.isVisible()
    with qtbot.waitSignal(wiz.swap_requested):
        wiz.swap_button.click()
