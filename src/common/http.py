"""API Gateway (REST, lambda-proxy) response helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.common.exceptions import AppError


def json_response(status_code: int, body: Mapping[str, object]) -> dict[str, object]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def error_response(error: AppError) -> dict[str, object]:
    return json_response(error.status_code, {"error": error.to_dict()})
