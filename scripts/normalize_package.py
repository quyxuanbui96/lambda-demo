"""Normalize Lambda ZIP paths and optionally keep only an explicit file list."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from zipfile import ZipFile


def normalize_package(archive_path: Path, *, include: set[str] | None = None) -> None:
    temporary = archive_path.with_suffix(".normalized.zip")
    try:
        with ZipFile(archive_path) as source, ZipFile(temporary, "w") as target:
            names = {info.orig_filename.replace("\\", "/") for info in source.infolist()}
            if include is not None and not include <= names:
                raise ValueError(f"Missing required package files: {sorted(include - names)}")
            for original in source.infolist():
                info = copy.copy(original)
                info.filename = original.orig_filename.replace("\\", "/")
                if include is not None and info.filename not in include:
                    continue
                target.writestr(info, source.read(original))
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Normalized Lambda package paths: {archive_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--include", nargs="+")
    args = parser.parse_args()
    normalize_package(args.archive, include=set(args.include) if args.include is not None else None)
