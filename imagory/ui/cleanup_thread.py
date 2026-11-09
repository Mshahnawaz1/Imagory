"""The cleanup worker thread: the only writer of the output database while a cleanup runs.

One thread runs one job, either the analysis (pipeline stages 1 to 5) or the copy (stages 1, 6
and 7). Each job creates its own :class:`~imagory.cleanup.pipeline.CleanupPipeline` inside
``run()``, because a pipeline's SQLite connection belongs to the thread that opened it. The
main window reads the main library's sources and the ``input_is_source`` flag on the main
thread and passes them in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QThread, Signal

from imagory import db
from imagory.cleanup.pipeline import CleanupPipeline

log = logging.getLogger(__name__)

Job = Literal["analysis", "copy"]


class CleanupThread(QThread):
    progressed = Signal(str, int, int)  # stage, done, total (total 0 means unknown)
    preview_ready = Signal(object)  # PreviewCounts
    finished_ok = Signal(object, int)  # DoneResult, cleanup run id
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        job: Job,
        input_root: Path,
        output_root: Path,
        library_root: Path,
        main_sources: Sequence[db.Source],
        input_is_source: bool = False,
        pipeline_options: dict[str, Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.job = job
        self.input_root = input_root
        self.output_root = output_root
        self._library_root = library_root
        self._main_sources = tuple(main_sources)
        self._input_is_source = input_is_source
        self._options = dict(pipeline_options or {})
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ask the running stage to stop. What is finished stays committed."""
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def _pipeline(self) -> CleanupPipeline:
        return CleanupPipeline(
            self.input_root,
            self.output_root,
            library_root=self._library_root,
            progress=self.progressed.emit,
            cancel_event=self._cancel,
            main_sources=self._main_sources,
            **self._options,
        )

    def run(self) -> None:
        try:
            with self._pipeline() as pipeline:
                if self.job == "analysis":
                    preview = pipeline.run_analysis()
                    if preview is None:
                        self.cancelled.emit()
                    else:
                        self.preview_ready.emit(preview)
                else:
                    result = pipeline.run_copy(self._input_is_source)
                    if result is None:
                        self.cancelled.emit()
                    else:
                        assert pipeline.run_id is not None
                        self.finished_ok.emit(result, pipeline.run_id)
        except Exception as exc:  # never crash the app; the wizard shows the message
            log.exception("Cleanup %s of %s failed", self.job, self.input_root)
            self.failed.emit(str(exc) or exc.__class__.__name__)
