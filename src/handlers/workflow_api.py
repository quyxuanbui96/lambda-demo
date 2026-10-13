"""API01–API08 entrypoint, independent from the legacy CSV demo."""

from __future__ import annotations

from collections.abc import Mapping

from src.workflow.api import request_id, response
from src.workflow.errors import WorkflowError
from src.workflow.runtime import get_runtime


def handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    try:
        return get_runtime().app.handle(event)
    except WorkflowError as error:
        return response(error.status, {"error": error.public()}, request_id(event))
    except Exception:
        return response(
            500,
            {"error": {"code": "E5000", "message": "Internal service error"}},
            request_id(event),
        )
