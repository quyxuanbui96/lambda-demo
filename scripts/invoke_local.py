#!/usr/bin/env python3
"""Run handlers locally against a test CSV using mock AWS context.

Usage:
  yarn invoke:s3    (simulates an S3 upload event)
  yarn invoke:api   (simulates calling POST /csv/import)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

# Add the project root to sys.path so we can import src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_env import load_local_environment


@dataclass
class DummyLambdaContext:
    function_name: str = "local-invoke"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:local:000000000000:function:local-invoke"
    aws_request_id: str = field(default_factory=lambda: str(uuid4()))

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


def main() -> None:
    parser = argparse.ArgumentParser(description="Invoke Lambda handlers locally.")
    parser.add_argument("handler", choices=["s3", "api"])
    parser.add_argument("--key", default="input/test.csv", help="S3 object key to process")
    args = parser.parse_args()

    load_local_environment()
    bucket = os.environ.get("CSV_BUCKET_NAME")
    if not bucket:
        print("ERROR: CSV_BUCKET_NAME is not set in the environment.", file=sys.stderr)
        print("Set CSV_BUCKET_NAME in .env (see .env.example).", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("POWERTOOLS_DEV", "1")
    from src.handlers import api, s3_csv_import

    ctx = DummyLambdaContext()

    if args.handler == "s3":
        event: dict[str, object] = {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "eventName": "ObjectCreated:Put",
                    "s3": {"bucket": {"name": bucket}, "object": {"key": quote_plus(args.key)}},
                }
            ]
        }
        print(f"Invoking s3_csv_import.handler with key: s3://{bucket}/{args.key} ...\n")
        try:
            result = s3_csv_import.handler(event, ctx)  # type: ignore[arg-type]
            print("\nResult:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as exc:
            print(f"\nHandler raised: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.handler == "api":
        event = {
            "httpMethod": "POST",
            "resource": "/csv/import",
            "path": "/csv/import",
            "body": json.dumps({"key": args.key}),
            "headers": {"Content-Type": "application/json"},
        }
        print(f"Invoking api.handler for POST /csv/import with key: {args.key} ...\n")
        try:
            result = api.handler(event, ctx)  # type: ignore[arg-type]
            print("\nResult:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if result.get("statusCode") != 200:
                sys.exit(1)
        except Exception as exc:
            print(
                f"\nHandler raised unhandled exception: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
