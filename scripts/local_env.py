"""Environment shared by local HTTP startup and direct handler invocation."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_local_environment(root: Path = ROOT) -> None:
    # Explicit shell values take priority; exported SSO credentials come next.
    load_dotenv(root / ".env.aws", override=False)
    load_dotenv(root / ".env", override=False)
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    os.environ.setdefault("STAGE", "local")
    os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "csv-lambda")
    os.environ.setdefault("POWERTOOLS_LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO"))
    if not os.environ.get("AWS_ACCESS_KEY_ID") and not os.environ.get("AWS_PROFILE"):
        profile = os.environ.get("AWS_SSO_PROFILE")
        if profile:
            os.environ["AWS_PROFILE"] = profile
