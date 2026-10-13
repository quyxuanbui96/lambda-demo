"""Stable workflow errors; dependency internals never become public messages."""

from __future__ import annotations


class WorkflowError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def public(self) -> dict[str, object]:
        value: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            value["details"] = self.details
        return value


def dependency_error() -> WorkflowError:
    return WorkflowError("E5002", "A dependency is temporarily unavailable", 503)


def forbidden() -> WorkflowError:
    return WorkflowError("E2002", "Access denied", 403)


def not_found() -> WorkflowError:
    return WorkflowError("E3001", "Resource not found", 404)


def conflict(message: str) -> WorkflowError:
    return WorkflowError("E3003", message, 409)
