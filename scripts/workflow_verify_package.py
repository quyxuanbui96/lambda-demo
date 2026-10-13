#!/usr/bin/env python3
"""Verify source isolation, health contents, Linux native ABI, template and artifact hashes."""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    directory = ROOT / ".serverless"
    archive = directory / "ocr-file-workflow.zip"
    with ZipFile(archive) as source:
        names = set(source.namelist())
        native = [name for name in names if ".so" in Path(name).name]
        if not native or not any(name.startswith("psycopg_binary/") for name in native):
            raise ValueError("Missing PostgreSQL native dependencies")
        for name in native:
            raw = source.read(name)
            if raw[:4] != b"\x7fELF" or struct.unpack_from("<H", raw, 18)[0] != 62:
                raise ValueError(f"Native library is not Linux x86_64: {name}")
        core = [name for name in native if name.startswith("pydantic_core/_pydantic_core")]
        if not any(
            name.startswith("psycopg-") and name.endswith(".dist-info/METADATA") for name in names
        ):
            raise ValueError("psycopg distribution metadata is required by SQLAlchemy")
        if len(core) != 1 or "cpython-314-x86_64-linux-gnu" not in core[0]:
            raise ValueError("pydantic_core is not CPython 3.14/Linux x86_64")
        expected = {
            path.relative_to(ROOT).as_posix() for path in (ROOT / "src/workflow").rglob("*.py")
        }
        expected |= {
            "src/__init__.py",
            "src/handlers/__init__.py",
            "src/handlers/workflow_api.py",
            "src/handlers/acquisition_worker.py",
            "src/handlers/workflow_maintenance.py",
        }
        actual = {name for name in names if name.startswith("src/") and name.endswith(".py")}
        if actual != expected or any(
            source.read(name) != (ROOT / name).read_bytes() for name in expected
        ):
            raise ValueError("Packaged source differs from the current workflow source")
        if any(
            name.startswith((".env", ".aws/", ".tools/", "tests/", "node_modules/")) or "\\" in name
            for name in names
        ):
            raise ValueError("Sensitive/local files or Windows paths in Lambda ZIP")
        if "certs/global-bundle.pem" not in names:
            raise ValueError("Missing PostgreSQL TLS certificate bundle")
    health = directory / "health.zip"
    with ZipFile(health) as source:
        if set(source.namelist()) != {
            "src/__init__.py",
            "src/handlers/__init__.py",
            "src/handlers/health.py",
        }:
            raise ValueError("Health must contain exactly its three standard-library source files")
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from src.handlers.health import handler;"
        'assert json.loads(handler({},None)["body"])=={"status":"ok"}'
    )
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(health)], cwd=directory, env={}, check=True
    )
    template = json.loads((directory / "cloudformation-template-update-stack.json").read_text())
    functions = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    ]
    if len(functions) != 4 or any(
        f["Runtime"] != "python3.14" or f["Architectures"] != ["x86_64"] for f in functions
    ):
        raise ValueError("Unexpected Lambda runtime or architecture")
    if (
        template["Resources"]["WorkflowBucket"]["Properties"]["VersioningConfiguration"]["Status"]
        != "Enabled"
    ):
        raise ValueError("Workflow S3 bucket must be versioned")
    report = {
        "runtime": "python3.14",
        "architecture": "x86_64",
        "native_libraries": len(native),
        "workflow_zip_bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "health_zip_bytes": health.stat().st_size,
        "source_matches_local": True,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
