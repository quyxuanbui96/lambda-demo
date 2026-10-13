from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import deploy


def test_local_stage_is_rejected_before_loading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_load() -> None:
        pytest.fail("Local stage must not load deployment credentials")

    monkeypatch.setattr(deploy, "load_local_environment", unexpected_load)
    with pytest.raises(ValueError, match="local is reserved"):
        deploy.deploy("deploy", "local")


def test_packaging_failure_prevents_aws_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serverless = tmp_path / "node_modules/serverless/run.js"
    serverless.parent.mkdir(parents=True)
    serverless.touch()
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "load_local_environment", lambda: None)
    monkeypatch.setattr(deploy.shutil, "which", lambda _: "/usr/bin/node")
    calls: list[list[str]] = []

    def run(args: list[str], *, cwd: Path, check: bool) -> None:
        calls.append(args)
        if "package" in args:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy("deploy", "dev")
    assert any("package" in args for args in calls)
    assert not any("deploy" in args for args in calls)


def test_workflow_deploy_requires_configuration_before_aws_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "load_local_environment", lambda: None)

    def unexpected_run(*args: object, **kwargs: object) -> None:
        pytest.fail("Deployment must not start without workflow configuration")

    monkeypatch.setattr(deploy.subprocess, "run", unexpected_run)
    with pytest.raises(ValueError, match="Workflow configuration"):
        deploy.deploy("deploy", "dev", "serverless.workflow.yml")
