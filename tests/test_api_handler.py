from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from src.handlers import api
from tests.conftest import BUCKET, FakeLambdaContext


def _event(path: str, body: object = None, *, method: str = "POST") -> dict[str, object]:
    return {
        "resource": path,
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "body": body if body is None or isinstance(body, str) else json.dumps(body),
        "isBase64Encoded": False,
        "requestContext": {"stage": "local"},
    }


def _call(event: dict[str, object], ctx: FakeLambdaContext) -> tuple[int, dict[str, Any]]:
    response = api.handler(event, ctx)
    return int(response["statusCode"]), json.loads(str(response["body"]))  # type: ignore[call-overload]


# --- /csv/upload-url ---------------------------------------------------------


def test_upload_url(s3: Any, lambda_context: FakeLambdaContext) -> None:
    status, body = _call(_event("/csv/upload-url", {"file_name": "売上 2024.csv"}), lambda_context)
    assert status == 200
    assert body["method"] == "PUT"
    assert body["headers"] == {"Content-Type": "text/csv"}
    assert body["expires_in"] == 900
    assert body["key"].startswith("input/") and body["key"].endswith("/売上 2024.csv")
    url = urlparse(body["upload_url"])
    assert BUCKET in url.netloc
    assert unquote(url.path).lstrip("/") == body["key"]


@pytest.mark.parametrize(
    "file_name",
    ["../evil.csv", "a/b.csv", "a\\b.csv", ".hidden.csv", "data.txt", "x\n.csv", "a.cs"],
)
def test_upload_url_rejects_bad_file_names(
    s3: Any, lambda_context: FakeLambdaContext, file_name: str
) -> None:
    status, body = _call(_event("/csv/upload-url", {"file_name": file_name}), lambda_context)
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "raw_body",
    [None, "", "not json", "[]", json.dumps({}), json.dumps({"file_name": "a.csv", "x": 1})],
)
def test_upload_url_rejects_bad_bodies(
    s3: Any, lambda_context: FakeLambdaContext, raw_body: str | None
) -> None:
    status, body = _call(_event("/csv/upload-url", raw_body), lambda_context)
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_base64_body_is_supported(s3: Any, lambda_context: FakeLambdaContext) -> None:
    event = _event("/csv/upload-url", base64.b64encode(b'{"file_name": "a.csv"}').decode())
    event["isBase64Encoded"] = True
    status, _ = _call(event, lambda_context)
    assert status == 200


def test_invalid_base64_body(s3: Any, lambda_context: FakeLambdaContext) -> None:
    event = _event("/csv/upload-url", "%%%not-base64%%%")
    event["isBase64Encoded"] = True
    status, _ = _call(event, lambda_context)
    assert status == 400


# --- /csv/import -------------------------------------------------------------


def test_import(s3: Any, lambda_context: FakeLambdaContext) -> None:
    s3.put_object(Bucket=BUCKET, Key="input/x/a.csv", Body=b"id,name,quantity\n1,A,1\n2,B,-5\n")
    status, body = _call(_event("/csv/import", {"key": "input/x/a.csv"}), lambda_context)
    assert status == 200
    assert body["total_rows"] == 2
    assert body["invalid_rows"] == 1
    assert body["errors"][0]["line_number"] == 3


@pytest.mark.parametrize(
    "key", ["other/a.csv", "input/a.txt", "input/../secret.csv", "input//a.csv", "input/./a.csv"]
)
def test_import_rejects_bad_keys(s3: Any, lambda_context: FakeLambdaContext, key: str) -> None:
    status, body = _call(_event("/csv/import", {"key": key}), lambda_context)
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_import_missing_object(s3: Any, lambda_context: FakeLambdaContext) -> None:
    status, body = _call(_event("/csv/import", {"key": "input/none.csv"}), lambda_context)
    assert status == 404
    assert body["error"]["code"] == "OBJECT_NOT_FOUND"


def test_import_bad_header(s3: Any, lambda_context: FakeLambdaContext) -> None:
    s3.put_object(Bucket=BUCKET, Key="input/h.csv", Body=b"a,b\n1,2\n")
    status, body = _call(_event("/csv/import", {"key": "input/h.csv"}), lambda_context)
    assert status == 422
    assert body["error"]["code"] == "CSV_FORMAT_ERROR"
    assert body["error"]["details"]["missing_columns"] == ["id", "name", "quantity"]


# --- routing / config --------------------------------------------------------


def test_unknown_route(s3: Any, lambda_context: FakeLambdaContext) -> None:
    status, body = _call(_event("/csv/unknown", {}), lambda_context)
    assert status == 404
    assert body["error"]["code"] == "ROUTE_NOT_FOUND"


def test_wrong_method(s3: Any, lambda_context: FakeLambdaContext) -> None:
    status, _ = _call(_event("/csv/import", method="GET"), lambda_context)
    assert status == 404


def test_missing_config_returns_500(
    s3: Any, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CSV_BUCKET_NAME")
    status, body = _call(_event("/csv/import", {"key": "input/a.csv"}), lambda_context)
    assert status == 500
    assert body["error"]["code"] == "CONFIG_ERROR"
