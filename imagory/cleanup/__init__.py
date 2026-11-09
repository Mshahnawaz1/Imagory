"""Takeout cleanup (PRD §8.10): a deduplicated, metadata-preserving copy of a Takeout export.

- :mod:`.types`: plain dataclasses and errors shared by every part, the wizard included
- :mod:`.checks`: input and output folder checks for the wizard
- :mod:`.inventory`: every file of the input, sorted into photos, other files and JSON
- :mod:`.planning`: pure rules for which copy is kept and where it goes
- :mod:`.hashing`: hash workers for files the scanner doesn't read
- :mod:`.copying`: the verified copy of one file
- :mod:`.export`: ``metadata.json`` and the text report
- :mod:`.pipeline`: :class:`~.pipeline.CleanupPipeline`, which runs the stages in order

This module imports nothing, so ``imagory.db`` can import :mod:`.types` without a cycle.
"""
