#!/usr/bin/env python3
"""Export the draft eight-API OpenAPI contract from the implemented request models."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.workflow.models import (
    AcquisitionRequest,
    DownloadRequest,
    EmptyRequest,
    Request,
    TransferRequest,
)


def document() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    models: list[type[Request]] = [
        AcquisitionRequest,
        DownloadRequest,
        TransferRequest,
        EmptyRequest,
    ]
    for model in models:
        value = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(value.pop("$defs", {}))
        schemas[model.__name__] = value
    uuid = {"type": "string", "format": "uuid"}
    timestamp = {"type": "string", "format": "date-time"}

    def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties) if required is None else required,
        }

    def ref(name: str) -> dict[str, Any]:
        return {"$ref": f"#/components/schemas/{name}"}

    schemas["Error"] = obj(
        {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "details": {
                "type": "array",
                "items": obj({"field": {"type": "string"}, "reason": {"type": "string"}}),
            },
        },
        ["code", "message"],
    )
    schemas["ErrorResponse"] = obj({"error": ref("Error"), "requestId": uuid})
    schemas["Organization"] = obj(
        {"organizationId": {"type": "string"}, "displayName": {"type": "string"}}
    )
    schemas["SessionResponse"] = obj(
        {
            "userId": {"type": "string"},
            "allowedOrganizations": {"type": "array", "items": ref("Organization")},
            "requestId": uuid,
        }
    )
    schemas["File"] = obj(
        {
            "fileId": uuid,
            "layoutVersion": {"type": "string"},
            "contentLength": {"type": "integer", "minimum": 1},
            "contentDigest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        }
    )
    schemas["SourceVersion"] = obj(
        {
            "fileId": uuid,
            "sourceSystem": {"enum": ["OCR", "TISI"]},
            "version": {"type": "string"},
            "businessYear": {"type": "integer"},
            "capturedAt": timestamp,
        }
    )
    common = {
        "acquisitionId": uuid,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "requestId": uuid,
    }
    schemas["AcquisitionPending"] = obj({**common, "status": {"enum": ["QUEUED", "RUNNING"]}})
    schemas["AcquisitionReady"] = obj(
        {
            **common,
            "status": {"const": "READY"},
            "sourceVersions": {"type": "array", "items": ref("SourceVersion")},
            "files": {"type": "array", "items": ref("File")},
        }
    )
    schemas["AcquisitionFailed"] = obj(
        {**common, "status": {"const": "FAILED"}, "error": ref("Error")}
    )
    schemas["AcquisitionResponse"] = {
        "oneOf": [ref(n) for n in ["AcquisitionPending", "AcquisitionReady", "AcquisitionFailed"]]
    }
    common = {
        "transferId": uuid,
        "acquisitionId": uuid,
        "fileId": uuid,
        "createdAt": timestamp,
        "requestId": uuid,
    }
    schemas["TransferPrepared"] = obj(
        {**common, "status": {"const": "PREPARED"}, "completedAt": {"type": "null"}}
    )
    schemas["TransferTransferred"] = obj(
        {**common, "status": {"const": "TRANSFERRED"}, "completedAt": timestamp}
    )
    schemas["TransferFailed"] = obj(
        {
            **common,
            "status": {"const": "FAILED"},
            "completedAt": {"type": "null"},
            "error": ref("Error"),
        }
    )
    schemas["TransferResponse"] = {
        "oneOf": [ref(n) for n in ["TransferPrepared", "TransferTransferred", "TransferFailed"]]
    }
    schemas["DownloadResponse"] = obj(
        {
            "downloadUrl": {"type": "string", "format": "uri"},
            "method": {"const": "GET"},
            "expiresAt": timestamp,
            "requestId": uuid,
        }
    )
    schemas["UploadResponse"] = obj(
        {
            "uploadUrl": {"type": "string", "format": "uri"},
            "method": {"const": "PUT"},
            "requiredHeaders": {"type": "object", "additionalProperties": {"type": "string"}},
            "expiresAt": timestamp,
            "requestId": uuid,
        }
    )
    definitions = [
        ("get", "/session/context", "API01", "SessionResponse", None, [200]),
        ("post", "/acquisitions", "API02", "AcquisitionResponse", "AcquisitionRequest", [202, 200]),
        ("get", "/acquisitions/{id}", "API03", "AcquisitionResponse", None, [200]),
        (
            "post",
            "/acquisitions/{id}/download-url",
            "API04",
            "DownloadResponse",
            "DownloadRequest",
            [200],
        ),
        ("post", "/transfers", "API05", "TransferResponse", "TransferRequest", [201, 200]),
        ("post", "/transfers/{id}/upload-url", "API06", "UploadResponse", "EmptyRequest", [200]),
        ("post", "/transfers/{id}/complete", "API07", "TransferResponse", "EmptyRequest", [200]),
        ("get", "/transfers/{id}", "API08", "TransferResponse", None, [200]),
    ]
    paths: dict[str, Any] = {}
    for method, path, api, output, input_model, statuses in definitions:
        responses = {
            str(status): {
                "description": "Success",
                "content": {"application/json": {"schema": ref(output)}},
            }
            for status in statuses
        }
        responses.update(
            {
                str(status): {
                    "description": "Validation/auth/state/dependency error",
                    "content": {"application/json": {"schema": ref("ErrorResponse")}},
                }
                for status in [400, 401, 403, 404, 409, 413, 422, 500, 503]
            }
        )
        operation: dict[str, Any] = {
            "operationId": api,
            "responses": responses,
            "parameters": [
                {"name": "X-Request-Id", "in": "header", "required": False, "schema": uuid}
            ],
            "security": [{"OcrCredential": []}],
        }
        if "{id}" in path:
            operation["parameters"].append(
                {"name": "id", "in": "path", "required": True, "schema": uuid}
            )
        if input_model:
            operation["requestBody"] = {
                "required": input_model != "EmptyRequest",
                "content": {"application/json": {"schema": ref(input_model)}},
            }
        paths.setdefault(path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "OCR/TISI acquisition and transfer",
            "version": "0.1.0",
            "description": (
                "Draft contract derived from WBS, pending formal Detail Design approval. "
                "Auth bypass is local/dev only; OCR verification is deferred."
            ),
        },
        "servers": [{"url": "http://127.0.0.1:3000", "description": "Local demo"}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "OcrCredential": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Existing OCR credential; no Lambda-issued JWT. "
                        "Development bypass ignores this header."
                    ),
                }
            },
        },
    }


if __name__ == "__main__":
    path = Path(__file__).resolve().parent.parent / "docs/openapi.workflow.json"
    path.write_text(json.dumps(document(), indent=2, ensure_ascii=False) + "\n")
    print(path.name)
