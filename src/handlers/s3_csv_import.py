"""S3 ObjectCreated trigger: parse + validate the uploaded CSV.

File-level failures are re-raised so the invocation is marked as failed (visible in metrics /
retried by Lambda async invocation). Row-level validation errors are logged and returned in the
summary — they are deterministic, so retrying would not help.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote_plus

from aws_lambda_powertools import Logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.common.config import load_settings
from src.common.exceptions import AppError, RequestValidationError
from src.common.storage import create_s3_client
from src.common.validation import format_validation_errors
from src.csv_processing.service import import_csv_from_s3

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()


class _S3Bucket(BaseModel):
    name: str = Field(min_length=1)


class _S3Object(BaseModel):
    # URL-encoded by S3 (spaces as "+", non-ASCII as %XX). Decode before use.
    key: str = Field(min_length=1)


class _S3Entity(BaseModel):
    bucket: _S3Bucket
    object_: _S3Object = Field(alias="object")


class _S3EventRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_source: str = Field(alias="eventSource", pattern=r"^aws:s3$")
    event_name: str = Field(alias="eventName")
    s3: _S3Entity


class _S3Event(BaseModel):
    records: list[_S3EventRecord] = Field(alias="Records", min_length=1)


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]:
    settings = load_settings()
    try:
        s3_event = _S3Event.model_validate(event)
    except ValidationError as exc:
        raise RequestValidationError(
            "Event is not a valid S3 event", details=format_validation_errors(exc)
        ) from exc

    client = create_s3_client()
    results: list[dict[str, object]] = []
    for record in s3_event.records:
        bucket = record.s3.bucket.name
        key = unquote_plus(record.s3.object_.key)
        logger.append_keys(bucket=bucket, key=key)
        logger.info("Processing CSV", extra={"event_name": record.event_name})
        try:
            summary = import_csv_from_s3(client, settings, bucket=bucket, key=key)
        except AppError as exc:
            logger.error("CSV import failed", extra={"error": exc.to_dict()})
            raise

        stats = summary.model_dump(mode="json", exclude={"errors", "bucket", "key"})
        if summary.invalid_rows:
            logger.warning(
                "CSV processed with invalid rows",
                extra={**stats, "row_errors": summary.model_dump(mode="json")["errors"]},
            )
        else:
            logger.info("CSV processed", extra=stats)
        results.append(summary.model_dump(mode="json"))

    return {"results": results}
