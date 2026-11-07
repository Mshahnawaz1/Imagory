import dataclasses
from pathlib import Path

import pytest

from imagory import cleanup_types as ct


def test_all_types_are_frozen_dataclasses():
    names = [
        "Candidate",
        "Rename",
        "PlannedCopy",
        "Plan",
        "CopyResult",
        "ExportOrigin",
        "ExportFile",
        "ExportRun",
        "ExportAlbum",
        "CleanupReport",
        "FolderCheck",
        "PreviewCounts",
        "DoneResult",
    ]
    for name in names:
        cls = getattr(ct, name)
        assert dataclasses.is_dataclass(cls), name
        assert cls.__dataclass_params__.frozen, name


def test_defaults_and_immutability():
    c = ct.Candidate(1, "photo", "a/b.jpg", "00" * 16, 10, None, False, None, None)
    assert c.in_output is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.size = 2  # type: ignore[misc]

    f = ct.ExportFile("2019/07/a.jpg", "photo", 1, "00" * 16)
    assert f.people == () and f.origins == () and f.favorite is False

    d = ct.DoneResult(0, (), (), True, Path("r.txt"), Path("out"), False)
    assert d.safe_to_delete
