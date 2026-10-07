from __future__ import annotations

import json
from pathlib import Path

import pytest
from mypy_boto3_s3 import S3Client

from scripts import invoke_local
from scripts.local_env import load_local_environment
from tests.conftest import BUCKET


@pytest.mark.parametrize("handler", ["api", "s3"])
def test_direct_invocation_loads_env_and_preserves_object_key(
    handler: str,
    s3: S3Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "input/orders + spaces.csv"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"id,name,quantity\n001,Apple,10\n")
    monkeypatch.delenv("CSV_BUCKET_NAME")
    monkeypatch.setenv("POWERTOOLS_DEV", "1")
    (tmp_path / ".env").write_text(f"CSV_BUCKET_NAME={BUCKET}\n", encoding="utf-8")
    monkeypatch.setattr(
        invoke_local, "load_local_environment", lambda: load_local_environment(tmp_path)
    )
    monkeypatch.setattr("sys.argv", ["invoke_local.py", handler, "--key", key])

    invoke_local.main()

    output = capsys.readouterr().out
    result = json.loads(output.split("\nResult:\n", 1)[1])
    summary = json.loads(result["body"]) if handler == "api" else result["results"][0]
    assert summary["valid_rows"] == 1
    assert summary["key"] == key
