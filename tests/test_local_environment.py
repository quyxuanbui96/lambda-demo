from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.local_env import load_local_environment


@pytest.fixture(autouse=True)
def isolate_local_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STAGE",
        "LOG_LEVEL",
        "POWERTOOLS_LOG_LEVEL",
        "CSV_ENCODING",
        "AWS_REGION",
        "AWS_PROFILE",
        "AWS_SSO_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_loads_env_and_exported_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "CSV_ENCODING=cp932\nAWS_REGION=us-east-1\nLOG_LEVEL=DEBUG\n", encoding="utf-8"
    )
    (tmp_path / ".env.aws").write_text(
        "export AWS_SESSION_TOKEN='temporary-token'\n", encoding="utf-8"
    )
    monkeypatch.delenv("AWS_SESSION_TOKEN")
    monkeypatch.delenv("AWS_DEFAULT_REGION")

    load_local_environment(tmp_path)

    assert os.environ["CSV_ENCODING"] == "cp932"
    assert os.environ["AWS_SESSION_TOKEN"] == "temporary-token"
    assert os.environ["AWS_DEFAULT_REGION"] == "us-east-1"
    assert os.environ["POWERTOOLS_LOG_LEVEL"] == "DEBUG"


def test_shell_values_take_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSV_ENCODING", "utf-8")
    (tmp_path / ".env").write_text("CSV_ENCODING=cp932\n", encoding="utf-8")
    (tmp_path / ".env.aws").write_text("AWS_ACCESS_KEY_ID=exported-key\n", encoding="utf-8")

    load_local_environment(tmp_path)

    assert os.environ["CSV_ENCODING"] == "utf-8"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "testing"


def test_uses_sso_profile_without_exported_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    (tmp_path / ".env").write_text("AWS_SSO_PROFILE=dev-profile\n", encoding="utf-8")

    load_local_environment(tmp_path)

    assert os.environ["AWS_PROFILE"] == "dev-profile"


def test_missing_bucket_stops_before_starting_serverless(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import run_local

    monkeypatch.delenv("CSV_BUCKET_NAME")
    monkeypatch.setattr(run_local, "load_local_environment", lambda: None)
    assert run_local.main() == 1
    assert "CSV_BUCKET_NAME" in capsys.readouterr().err
