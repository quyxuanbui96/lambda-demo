#!/usr/bin/env python3
"""Import trusted, version-pinned OCR/TISI source manifest into the catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_env import load_local_environment
from src.workflow.models import SourceVersion
from src.workflow.runtime import get_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    values = [
        SourceVersion.model_validate(value) for value in json.loads(args.manifest.read_text())
    ]
    load_local_environment()
    runtime = get_runtime()
    with runtime.app.service.database.transaction() as repo:
        for value in values:
            settings = runtime.app.service.settings
            if value.bucket != settings.bucket or not value.key.startswith(settings.source_prefix):
                raise ValueError("Source manifest contains an unapproved bucket/prefix")
            repo.register_source(value)
    print(f"Registered {len(values)} source versions")


if __name__ == "__main__":
    main()
