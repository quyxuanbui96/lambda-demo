"""Helpers to turn Pydantic validation errors into a stable JSON shape."""

from __future__ import annotations

from pydantic import ValidationError


def format_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in err["loc"]) or "<root>",
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
