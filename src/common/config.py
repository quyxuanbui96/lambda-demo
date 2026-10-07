"""Runtime configuration loaded from environment variables (set via serverless.yml / .env).

Rules:
- ``CSV_BUCKET_NAME`` is required.
- Other variables have documented defaults, but if a variable is *present* it must be valid
  (e.g. ``CSV_ENCODING=""`` is an error, not "use the default").
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.common.exceptions import ConfigError
from src.common.validation import format_validation_errors

CsvEncoding = Literal["utf-8", "utf-8-sig", "cp932", "shift_jis"]

_ENV_TO_FIELD: dict[str, str] = {
    "CSV_BUCKET_NAME": "csv_bucket_name",
    "CSV_INPUT_PREFIX": "csv_input_prefix",
    "CSV_ENCODING": "csv_encoding",
    "CSV_MAX_BYTES": "csv_max_bytes",
    "CSV_MAX_REPORTED_ERRORS": "csv_max_reported_errors",
    "PRESIGNED_URL_EXPIRES_SECONDS": "presigned_url_expires_seconds",
}
_FIELD_TO_ENV: dict[str, str] = {field: env for env, field in _ENV_TO_FIELD.items()}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    csv_bucket_name: str = Field(min_length=3, max_length=63)
    csv_input_prefix: str = Field(default="input/", min_length=2, max_length=256)
    csv_encoding: CsvEncoding = "utf-8-sig"
    csv_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    csv_max_reported_errors: int = Field(default=100, ge=1, le=1000)
    presigned_url_expires_seconds: int = Field(default=900, ge=60, le=3600)

    @field_validator("csv_input_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if value.startswith("/") or not value.endswith("/"):
            raise ValueError("must end with '/' and must not start with '/'")
        return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    raw = {field: env[name] for name, field in _ENV_TO_FIELD.items() if name in env}
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        details = [
            {**err, "field": _FIELD_TO_ENV.get(err["field"], err["field"])}
            for err in format_validation_errors(exc)
        ]
        raise ConfigError(
            "Invalid Lambda configuration (environment variables)", details=details
        ) from exc
