"""Public liveness endpoint with no third-party or AWS dependencies."""

import json


def handler(event: dict[str, object], context: object) -> dict[str, object]:
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": json.dumps({"status": "ok"}),
    }
