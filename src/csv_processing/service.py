"""CSV import use case shared by the S3 trigger and the HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.common.config import Settings
from src.common.exceptions import RequestValidationError
from src.common.storage import read_object_bytes
from src.csv_processing.models import CsvImportSummary, SampleRecord
from src.csv_processing.parser import ParsedCsv, parse_csv

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def validate_input_key(key: str, *, input_prefix: str) -> str:
    if not key.startswith(input_prefix):
        raise RequestValidationError(f"key must start with '{input_prefix}'", details={"key": key})
    if not key.lower().endswith(".csv"):
        raise RequestValidationError("key must end with '.csv'", details={"key": key})
    segments = key[len(input_prefix) :].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise RequestValidationError("key contains an invalid path segment", details={"key": key})
    return key


def import_csv_from_s3(
    client: S3Client, settings: Settings, *, bucket: str, key: str
) -> CsvImportSummary:
    if bucket != settings.csv_bucket_name:
        raise RequestValidationError(
            "bucket is not the configured CSV bucket", details={"bucket": bucket}
        )
    validate_input_key(key, input_prefix=settings.csv_input_prefix)

    data = read_object_bytes(client, bucket=bucket, key=key, max_bytes=settings.csv_max_bytes)
    parsed = parse_csv(data, SampleRecord, encoding=settings.csv_encoding)

    # Extension point: persist `parsed.records` (e.g. DynamoDB / RDS) here.
    # Decide the policy for partially invalid files first (reject whole file vs. import only
    # valid rows) — it is a business decision, so it is intentionally not implemented yet.

    return _build_summary(
        parsed, bucket=bucket, key=key, max_errors=settings.csv_max_reported_errors
    )


def _build_summary(
    parsed: ParsedCsv[SampleRecord], *, bucket: str, key: str, max_errors: int
) -> CsvImportSummary:
    reported = parsed.row_errors[:max_errors]
    return CsvImportSummary(
        bucket=bucket,
        key=key,
        total_rows=parsed.total_rows,
        valid_rows=len(parsed.records),
        invalid_rows=len(parsed.row_errors),
        errors=reported,
        errors_truncated=len(parsed.row_errors) > len(reported),
    )
