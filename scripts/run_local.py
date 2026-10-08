#!/usr/bin/env python3
"""Start serverless-offline with the active Poetry Python on Windows/macOS/Linux."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_env import ROOT, load_local_environment
from src.common.config import load_settings
from src.common.exceptions import ConfigError


def main() -> int:
    load_local_environment()
    try:
        load_settings()
    except ConfigError as exc:
        print(f"ERROR: {exc.message}: {exc.details}", file=sys.stderr)
        print("Set CSV_BUCKET_NAME in .env (see .env.example).", file=sys.stderr)
        return 1

    node = shutil.which("node")
    serverless = ROOT / "node_modules" / "serverless" / "run.js"
    if not node or not serverless.is_file():
        print("ERROR: Node.js / Serverless not found. Run 'yarn install' first.", file=sys.stderr)
        return 1

    os.environ.pop("PORT", None)
    # serverless-offline selects Python from PATH / VIRTUAL_ENV.
    os.environ["VIRTUAL_ENV"] = sys.prefix
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    print("Starting CSV API at http://127.0.0.1:3000/local/csv/ ...", flush=True)
    command = [
        node,
        str(serverless),
        "offline",
        "start",
        "--stage",
        "local",
        *sys.argv[1:],
    ]
    try:
        return subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
