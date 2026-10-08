from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_health_runs_without_site_packages_or_aws_configuration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    # Isolated Python disables site-packages, PYTHONPATH and the current directory.
    # An empty environment ensures the handler does not depend on AWS / CSV config.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from src.handlers.health import handler; "
            "print(json.dumps(handler({'httpMethod':'GET','resource':'/health'},None)))",
            str(root),
        ],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
        check=True,
    )
    response = json.loads(result.stdout)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("application/json")
    assert response["headers"]["Cache-Control"] == "no-store"
    assert json.loads(response["body"]) == {"status": "ok"}
