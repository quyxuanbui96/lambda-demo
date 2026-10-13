#!/usr/bin/env python3
"""Package and deploy the service on macOS/Linux using the project environment."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_env import ROOT, load_local_environment
from src.common.config import load_settings


def deploy(task: str, stage: str, configuration: str = "serverless.yml") -> None:
    if stage == "local" or re.fullmatch(r"[a-zA-Z0-9-]+", stage) is None:
        raise ValueError("Specify a deploy stage such as dev; local is reserved for offline use.")

    load_local_environment()
    if configuration == "serverless.yml":
        load_settings()
    elif task == "deploy":
        from src.workflow.config import load_workflow_settings
        from src.workflow.errors import WorkflowError

        os.environ["STAGE"] = stage
        try:
            load_workflow_settings()
        except WorkflowError as error:
            raise ValueError(error.message) from None
        if not os.environ.get("WF_VPC_SUBNET_IDS") or not os.environ.get(
            "WF_VPC_SECURITY_GROUP_IDS"
        ):
            raise ValueError(
                "Configure workflow private-RDS VPC subnets and security groups first."
            )
    os.environ["VIRTUAL_ENV"] = sys.prefix
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    os.environ.setdefault("SLS_TELEMETRY_DISABLED", "1")
    if bundle := os.environ.get("AWS_CA_BUNDLE"):
        if not Path(bundle).is_file():
            raise ValueError("AWS_CA_BUNDLE file not found.")
        os.environ["NODE_EXTRA_CA_CERTS"] = bundle
        os.environ["PIP_CERT"] = bundle

    node = shutil.which("node")
    serverless = ROOT / "node_modules" / "serverless" / "run.js"
    if not node or not serverless.is_file():
        raise ValueError("Node.js / Serverless not found. Run yarn install first.")
    command = [node, str(serverless)]
    options = ["--stage", stage, "--region", os.environ.get("AWS_REGION", "ap-southeast-2")]
    if configuration != "serverless.yml":
        options.extend(["--config", configuration])

    def run(args: list[str]) -> None:
        subprocess.run(args, cwd=ROOT, check=True)

    if task == "info":
        run([*command, "info", *options])
        return

    run(["poetry", "export", "--help"])
    run(["docker", "version", "--format", "{{.Server.Version}}"])
    run([*command, "package", *options])
    if task == "deploy":
        run([*command, "deploy", "--package", ".serverless", *options])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-Task", choices=["package", "deploy", "info"], default="deploy")
    parser.add_argument("-Stage", default="dev")
    parser.add_argument(
        "-Config", choices=["serverless.yml", "serverless.workflow.yml"], default="serverless.yml"
    )
    args = parser.parse_args()
    try:
        deploy(args.Task, args.Stage, args.Config)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
