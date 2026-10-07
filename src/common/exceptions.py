"""Application error hierarchy.

Every error carries an explicit HTTP status and a stable machine-readable ``code`` so that
API responses and logs are consistent across handlers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

ErrorDetails = Mapping[str, object] | Sequence[Mapping[str, object]]


class AppError(Exception):
    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, details: ErrorDetails | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class ConfigError(AppError):
    status_code = 500
    code = "CONFIG_ERROR"


class RequestValidationError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"


class RouteNotFoundError(AppError):
    status_code = 404
    code = "ROUTE_NOT_FOUND"


class ObjectNotFoundError(AppError):
    status_code = 404
    code = "OBJECT_NOT_FOUND"


class FileTooLargeError(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class CsvFormatError(AppError):
    """The file as a whole is unusable (encoding, header, empty, malformed quoting...)."""

    status_code = 422
    code = "CSV_FORMAT_ERROR"


class StorageError(AppError):
    """S3 failed for a reason other than "not found" (permissions, throttling, network...)."""

    status_code = 502
    code = "STORAGE_ERROR"
