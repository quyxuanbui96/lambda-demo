"""Normalize ZIP paths emitted by the requirements plugin on Windows for Linux."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from zipfile import ZipFile


def normalize_package(archive_path: Path) -> None:
    temporary = archive_path.with_suffix(".normalized.zip")
    try:
        with ZipFile(archive_path) as source, ZipFile(temporary, "w") as target:
            for original in source.infolist():
                info = copy.copy(original)
                info.filename = original.orig_filename.replace("\\", "/")
                target.writestr(info, source.read(original))
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Normalized Lambda package paths: {archive_path.name}")


if __name__ == "__main__":
    normalize_package(Path(sys.argv[1]))
