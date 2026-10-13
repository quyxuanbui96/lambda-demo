"""SQS batch entrypoint with partial batch failure reporting."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from uuid import UUID

from src.workflow.runtime import get_runtime

logger = logging.getLogger("workflow")


def handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("Expected SQS records")
    failures: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("messageId"), str):
            raise ValueError("Invalid SQS record")
        try:
            payload = json.loads(record["body"])
            get_runtime().worker.run(UUID(payload["acquisitionId"]), UUID(payload["requestId"]))
        except Exception:
            # Do not log message body, exception text, credentials or file contents.
            logger.warning(
                json.dumps(
                    {
                        "action": "acquisition.worker",
                        "messageId": record["messageId"],
                        "result": "RETRY",
                    }
                )
            )
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
