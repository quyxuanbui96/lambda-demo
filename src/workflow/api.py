"""Eight-route Lambda application with validation and safe structured responses."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Literal
from uuid import UUID, uuid4

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import ValidationError

from src.workflow.auth import Authenticator
from src.workflow.errors import WorkflowError
from src.workflow.models import (
    AcquisitionRequest,
    DownloadRequest,
    EmptyRequest,
    Request,
    TransferRequest,
)
from src.workflow.service import WorkflowService

logger = logging.getLogger("workflow")
logger.setLevel(logging.INFO)
Route = Literal[
    "session",
    "acquisition-create",
    "acquisition-get",
    "download",
    "transfer-create",
    "transfer-get",
    "upload",
    "complete",
]
ROUTES: list[tuple[str, re.Pattern[str], Route]] = [
    ("GET", re.compile(r"^/session/context$"), "session"),
    ("POST", re.compile(r"^/acquisitions$"), "acquisition-create"),
    ("GET", re.compile(r"^/acquisitions/([^/]+)$"), "acquisition-get"),
    ("POST", re.compile(r"^/acquisitions/([^/]+)/download-url$"), "download"),
    ("POST", re.compile(r"^/transfers$"), "transfer-create"),
    ("GET", re.compile(r"^/transfers/([^/]+)$"), "transfer-get"),
    ("POST", re.compile(r"^/transfers/([^/]+)/upload-url$"), "upload"),
    ("POST", re.compile(r"^/transfers/([^/]+)/complete$"), "complete"),
]


def request_id(event: Mapping[str, object]) -> UUID:
    headers = event.get("headers")
    if isinstance(headers, dict):
        values = [
            v for k, v in headers.items() if isinstance(k, str) and k.lower() == "x-request-id"
        ]
        if len(values) == 1 and isinstance(values[0], str):
            try:
                return UUID(values[0])
            except ValueError:
                pass
    return uuid4()


def response(status: int, body: Mapping[str, object], correlation: UUID) -> dict[str, object]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Request-Id": str(correlation),
        },
        "body": json.dumps({**body, "requestId": str(correlation)}, ensure_ascii=False),
    }


def parse_body[T: Request](event: Mapping[str, object], model: type[T]) -> T:
    raw = event.get("body")
    if (raw is None or raw == "") and model is EmptyRequest:
        return model.model_validate({})
    if not isinstance(raw, str) or not raw or len(raw) > 64 * 1024:
        raise WorkflowError("E1001", "A valid JSON request body is required")
    try:
        if event.get("isBase64Encoded") is True:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        value = json.loads(raw)
        return model.model_validate(value)
    except ValidationError as error:
        details = [
            {"field": ".".join(map(str, item["loc"])), "reason": item["type"]}
            for item in error.errors(include_input=False)
        ]
        raise WorkflowError("E1001", "Invalid request fields", details=details) from None
    except ValueError, UnicodeError, binascii.Error:
        raise WorkflowError("E1001", "A valid JSON request body is required") from None


def route(event: Mapping[str, object], stage: str) -> tuple[Route, UUID | None]:
    method = event.get("httpMethod")
    context = event.get("requestContext")
    if not isinstance(method, str) and isinstance(context, dict):
        http = context.get("http")
        method = http.get("method") if isinstance(http, dict) else None
    path = event.get("path", event.get("rawPath"))
    if not isinstance(path, str):
        raise WorkflowError("E1004", "Route not found", 404)
    if path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1 :]
    for expected, pattern, name in ROUTES:
        match = pattern.fullmatch(path)
        if method == expected and match:
            if not match.groups():
                return name, None
            try:
                return name, UUID(match.group(1))
            except ValueError:
                raise WorkflowError("E1001", "Resource ID must be a UUID") from None
    raise WorkflowError("E1004", "Route not found", 404)


class WorkflowApplication:
    def __init__(self, service: WorkflowService, authenticator: Authenticator) -> None:
        self.service, self.authenticator = service, authenticator

    def handle(self, event: Mapping[str, object]) -> dict[str, object]:
        correlation, started = request_id(event), time.perf_counter()
        action: str = "unknown"
        user: str | None = None
        status, error_code = 500, None
        resource: UUID | None = None
        try:
            action, resource = route(event, self.service.settings.stage)
            context = self.authenticator.authenticate(event)
            user = context.userId
            if (
                event.get("queryStringParameters")
                or event.get("multiValueQueryStringParameters")
                or event.get("rawQueryString")
            ):
                raise WorkflowError("E1001", "Query parameters are not accepted")
            if action in {"session", "acquisition-get", "transfer-get"} and event.get(
                "body"
            ) not in (None, ""):
                raise WorkflowError("E1001", "This API does not accept a request body")
            if action == "session":
                status, body = 200, context.model_dump(mode="json")
            elif action == "acquisition-create":
                status, body = self.service.create_acquisition(
                    parse_body(event, AcquisitionRequest), context, correlation
                )
            elif action == "transfer-create":
                status, body = self.service.create_transfer(
                    parse_body(event, TransferRequest), context, correlation
                )
            elif resource is None:
                raise WorkflowError("E1004", "Route not found", 404)
            elif action == "acquisition-get":
                status, body = 200, self.service.get_acquisition(resource, context)
            elif action == "transfer-get":
                status, body = 200, self.service.get_transfer(resource, context)
            elif action == "download":
                status, body = 200, self.service.download(
                    resource, parse_body(event, DownloadRequest).fileId, context, correlation
                )
            elif action == "upload":
                parse_body(event, EmptyRequest)
                status, body = 200, self.service.upload(resource, context, correlation)
            else:
                parse_body(event, EmptyRequest)
                status, body = 200, self.service.complete(resource, context, correlation)
            result = response(status, body, correlation)
            if action in {"acquisition-get", "transfer-get"} and body.get("status") in {
                "QUEUED",
                "RUNNING",
                "PREPARED",
            }:
                headers = result["headers"]
                assert isinstance(headers, dict)
                headers["Retry-After"] = str(self.service.settings.poll_interval_seconds)
            return result
        except WorkflowError as error:
            status, error_code = error.status, error.code
            return response(status, {"error": error.public()}, correlation)
        except Exception:
            status, error_code = 500, "E5000"
            return response(
                status,
                {"error": {"code": "E5000", "message": "Internal service error"}},
                correlation,
            )
        finally:
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                json.dumps(
                    {
                        "action": action,
                        "requestId": str(correlation),
                        "userId": user,
                        "resourceId": str(resource) if resource else None,
                        "httpStatus": status,
                        "errorCode": error_code,
                        "durationMs": elapsed,
                        "authBypassed": self.service.settings.auth_mode == "bypass",
                    }
                )
            )
            metrics = Metrics(namespace="OCRFileWorkflow", service="workflow")
            metrics.add_dimension(name="Stage", value=self.service.settings.stage)
            metrics.add_metric(name="APIRequests", unit=MetricUnit.Count, value=1)
            metrics.add_metric(name="APILatency", unit=MetricUnit.Milliseconds, value=elapsed)
            if status >= 500:
                metrics.add_metric(name="APIServerErrors", unit=MetricUnit.Count, value=1)
            metrics.flush_metrics()
