from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

BUCKET = "test-csv-bucket"
REGION = "ap-northeast-1"

_MANAGED_ENV_PREFIXES = ("AWS_", "CSV_", "WF_")
_MANAGED_ENV_NAMES = ("PRESIGNED_URL_EXPIRES_SECONDS",)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let tests touch real AWS: wipe developer credentials and set fake ones."""
    for name in list(os.environ):
        if name.startswith(_MANAGED_ENV_PREFIXES) or name in _MANAGED_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("CSV_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "csv-lambda-test")


@pytest.fixture
def s3() -> Iterator[object]:
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


@dataclass
class FakeLambdaContext:
    function_name: str = "test-function"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:123456789012:function:test-function"
    aws_request_id: str = field(default_factory=lambda: str(uuid4()))

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


@pytest.fixture
def lambda_context() -> FakeLambdaContext:
    return FakeLambdaContext()
