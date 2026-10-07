from __future__ import annotations

from typing import Any

import pytest

from src.common.exceptions import (
    CsvFormatError,
    FileTooLargeError,
    ObjectNotFoundError,
    RequestValidationError,
)
from src.handlers import s3_csv_import
from tests.conftest import BUCKET, FakeLambdaContext

VALID_CSV = "id,name,quantity\n1,りんご,3\n2,Banana,0\n".encode()


def _s3_event(key: str, bucket: str = BUCKET) -> dict[str, object]:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": bucket}, "object": {"key": key}},
            }
        ]
    }


def test_valid_file(s3: Any, lambda_context: FakeLambdaContext) -> None:
    s3.put_object(Bucket=BUCKET, Key="input/a.csv", Body=VALID_CSV)
    result = s3_csv_import.handler(_s3_event("input/a.csv"), lambda_context)
    assert result == {
        "results": [
            {
                "bucket": BUCKET,
                "key": "input/a.csv",
                "total_rows": 2,
                "valid_rows": 2,
                "invalid_rows": 0,
                "errors": [],
                "errors_truncated": False,
            }
        ]
    }


def test_url_encoded_key_is_decoded(s3: Any, lambda_context: FakeLambdaContext) -> None:
    real_key = "input/my file 売上.csv"
    s3.put_object(Bucket=BUCKET, Key=real_key, Body=VALID_CSV)
    encoded = "input/my+file+%E5%A3%B2%E4%B8%8A.csv"
    result = s3_csv_import.handler(_s3_event(encoded), lambda_context)
    assert result["results"][0]["key"] == real_key  # type: ignore[index]


def test_invalid_rows_are_reported_and_truncated(
    s3: Any, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CSV_MAX_REPORTED_ERRORS", "2")
    body = b"id,name,quantity\n1,A,-1\n2,B,x\n3,,1\n4,D,4\n"
    s3.put_object(Bucket=BUCKET, Key="input/bad.csv", Body=body)
    summary = s3_csv_import.handler(_s3_event("input/bad.csv"), lambda_context)["results"][0]  # type: ignore[index]
    assert summary["valid_rows"] == 1
    assert summary["invalid_rows"] == 3
    assert len(summary["errors"]) == 2
    assert summary["errors_truncated"] is True


def test_header_error_raises(s3: Any, lambda_context: FakeLambdaContext) -> None:
    s3.put_object(Bucket=BUCKET, Key="input/h.csv", Body=b"foo,bar\n1,2\n")
    with pytest.raises(CsvFormatError):
        s3_csv_import.handler(_s3_event("input/h.csv"), lambda_context)


def test_missing_object_raises(s3: Any, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(ObjectNotFoundError):
        s3_csv_import.handler(_s3_event("input/none.csv"), lambda_context)


def test_file_too_large_raises(
    s3: Any, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CSV_MAX_BYTES", "10")
    s3.put_object(Bucket=BUCKET, Key="input/big.csv", Body=VALID_CSV)
    with pytest.raises(FileTooLargeError):
        s3_csv_import.handler(_s3_event("input/big.csv"), lambda_context)


def test_foreign_bucket_rejected(s3: Any, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(RequestValidationError, match="bucket"):
        s3_csv_import.handler(_s3_event("input/a.csv", bucket="other"), lambda_context)


def test_key_outside_prefix_rejected(s3: Any, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(RequestValidationError, match="must start with"):
        s3_csv_import.handler(_s3_event("other/a.csv"), lambda_context)


@pytest.mark.parametrize(
    "event",
    [{}, {"Records": []}, {"Records": [{"eventSource": "aws:sqs"}]}],
)
def test_invalid_event_rejected(
    s3: Any, lambda_context: FakeLambdaContext, event: dict[str, object]
) -> None:
    with pytest.raises(RequestValidationError, match="not a valid S3 event"):
        s3_csv_import.handler(event, lambda_context)
