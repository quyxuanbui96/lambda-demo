"""HTTP API (API Gateway REST, lambda-proxy).

Routes:
  POST /csv/upload-url  {"file_name": "orders.csv"}
      -> presigned S3 PUT URL under CSV_INPUT_PREFIX. Uploading to it fires the S3 trigger.
  POST /csv/import      {"key": "input/.../orders.csv"}
      -> synchronous parse + validate of an object already in S3; returns the summary.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.common.config import Settings, load_settings
from src.common.exceptions import AppError, RequestValidationError, RouteNotFoundError
from src.common.http import error_response, json_response
from src.common.storage import create_s3_client, generate_presigned_put_url
from src.common.validation import format_validation_errors
from src.csv_processing.service import import_csv_from_s3

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from mypy_boto3_s3 import S3Client

logger = Logger()

CSV_CONTENT_TYPE = "text/csv"

type RouteHandler = Callable[[Mapping[str, object], Settings, S3Client], dict[str, object]]


class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file_name: str = Field(min_length=5, max_length=200)

    @field_validator("file_name")
    @classmethod
    def _validate_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("must not contain path separators")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("must not contain control characters")
        if value.startswith("."):
            raise ValueError("must not start with '.'")
        if not value.lower().endswith(".csv"):
            raise ValueError("must end with '.csv'")
        return value


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=1024)


def _parse_body[RequestT: BaseModel](
    event: Mapping[str, object], model: type[RequestT]
) -> RequestT:
    body = event.get("body")
    if body is None or body == "":
        raise RequestValidationError("Request body is required")
    if not isinstance(body, str):
        raise RequestValidationError("Request body must be a JSON string")
    if event.get("isBase64Encoded") is True:
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise RequestValidationError("Request body is not valid base64 UTF-8") from exc
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            "Invalid request body", details=format_validation_errors(exc)
        ) from exc


def _create_upload_url(
    event: Mapping[str, object], settings: Settings, client: S3Client
) -> dict[str, object]:
    request = _parse_body(event, UploadUrlRequest)
    # uuid folder: unique key per upload, original file name kept for traceability.
    key = f"{settings.csv_input_prefix}{uuid.uuid4().hex}/{request.file_name}"
    url = generate_presigned_put_url(
        client,
        bucket=settings.csv_bucket_name,
        key=key,
        content_type=CSV_CONTENT_TYPE,
        expires_in=settings.presigned_url_expires_seconds,
    )
    return json_response(
        200,
        {
            "upload_url": url,
            "method": "PUT",
            "headers": {"Content-Type": CSV_CONTENT_TYPE},
            "key": key,
            "expires_in": settings.presigned_url_expires_seconds,
        },
    )


def _import_csv(
    event: Mapping[str, object], settings: Settings, client: S3Client
) -> dict[str, object]:
    request = _parse_body(event, ImportRequest)
    summary = import_csv_from_s3(client, settings, bucket=settings.csv_bucket_name, key=request.key)
    return json_response(200, summary.model_dump(mode="json"))


ROUTES: dict[tuple[str, str], RouteHandler] = {
    ("POST", "/csv/upload-url"): _create_upload_url,
    ("POST", "/csv/import"): _import_csv,
}


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]:
    method = str(event.get("httpMethod", ""))
    resource = str(event.get("resource", ""))
    logger.append_keys(http_method=method, resource=resource)
    try:
        route = ROUTES.get((method, resource))
        if route is None:
            raise RouteNotFoundError(f"No route for {method} {resource}")
        return route(event, load_settings(), create_s3_client())
    except AppError as exc:
        log = logger.error if exc.status_code >= 500 else logger.warning
        log("Request failed", extra={"error": exc.to_dict()})
        return error_response(exc)
    except Exception:
        logger.exception("Unhandled error")
        return error_response(AppError("Internal server error"))
