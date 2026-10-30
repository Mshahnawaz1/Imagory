"""Write THIRD_PARTY_LICENSES.txt from the licence files of the bundled packages (PRD §13).

Usage: uv run python scripts/collect_licenses.py
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

BUNDLED = ["PySide6_Essentials", "shiboken6", "pillow", "pillow_heif"]
OUT = Path(__file__).resolve().parents[1] / "THIRD_PARTY_LICENSES.txt"

HEADER = """\
Third-party components bundled in the Imagory executable
=========================================================

Imagory's own code is released under the MIT licence (see LICENSE). The executable also
contains the components below, which keep their own licences. Their licence texts follow.

PySide6 / Qt is used under the LGPL v3. Imagory's source code and build instructions are
public, so you can rebuild Imagory against a different Qt version.

NOTE (open question, PRD section 18): the pillow-heif binary wheels bundle libheif and
libde265 (LGPLv3) and x265 (GPLv2), which makes the pillow-heif binary GPLv2 as a whole.
This must be resolved before the first public release.
"""


def license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    found = []
    for file in dist.files or []:
        name = file.name.upper()
        if any(key in name for key in ("LICENSE", "LICENCE", "COPYING")):
            found.append(
                (str(file), Path(file.locate()).read_text(encoding="utf-8", errors="replace"))
            )
    return found


# PySide6 wheels ship no licence file; use the standard texts installed on Debian/Ubuntu.
COMMON_LICENSES = Path("/usr/share/common-licenses")
QT_LICENSE_TEXTS = ["LGPL-3", "GPL-3"]  # the LGPL v3 is a set of additions to the GPL v3


def main() -> None:
    parts = [HEADER]
    for name in BUNDLED:
        dist = metadata.distribution(name)
        meta = dist.metadata
        licence = meta.get("License-Expression") or meta.get("License") or "see below"
        parts.append(f"\n{'=' * 78}\n{dist.name} {dist.version}  ({licence.splitlines()[0]})\n")
        parts.append(f"{meta.get('Home-page') or meta.get('Project-URL') or ''}\n{'=' * 78}\n")
        files = license_files(dist)
        if not files and name == "PySide6_Essentials":
            for text_name in QT_LICENSE_TEXTS:
                text_path = COMMON_LICENSES / text_name
                if not text_path.exists():
                    raise SystemExit(f"{text_path} not found; add the {text_name} text by hand.")
                files.append((text_name, text_path.read_text(encoding="utf-8")))
        if not files:
            parts.append("(no licence file shipped in the package metadata)\n")
        for path, text in files:
            parts.append(f"\n--- {path} ---\n\n{text.strip()}\n")
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
