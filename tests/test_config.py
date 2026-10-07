from __future__ import annotations

import pytest

from src.common.config import load_settings
from src.common.exceptions import ConfigError


def test_defaults_when_only_bucket_is_set() -> None:
    settings = load_settings({"CSV_BUCKET_NAME": "my-bucket"})
    assert settings.csv_input_prefix == "input/"
    assert settings.csv_encoding == "utf-8-sig"
    assert settings.csv_max_bytes == 10 * 1024 * 1024


def test_numeric_env_values_are_parsed() -> None:
    settings = load_settings({"CSV_BUCKET_NAME": "my-bucket", "CSV_MAX_BYTES": "2048"})
    assert settings.csv_max_bytes == 2048


def test_missing_bucket_fails_with_env_name() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings({})
    assert exc_info.value.details == [{"field": "CSV_BUCKET_NAME", "message": "Field required"}]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CSV_ENCODING", ""),
        ("CSV_ENCODING", "latin-1"),
        ("CSV_INPUT_PREFIX", "input"),
        ("CSV_INPUT_PREFIX", "/input/"),
        ("CSV_MAX_BYTES", "0"),
        ("CSV_MAX_BYTES", "abc"),
        ("CSV_MAX_REPORTED_ERRORS", "0"),
        ("PRESIGNED_URL_EXPIRES_SECONDS", "999999"),
    ],
)
def test_present_but_invalid_values_are_rejected(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings({"CSV_BUCKET_NAME": "my-bucket", name: value})
    details = exc_info.value.details
    assert isinstance(details, list)
    assert details[0]["field"] == name
