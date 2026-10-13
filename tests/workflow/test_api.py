from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

import pytest

from src.workflow.auth import GatewayAuthenticator
from src.workflow.models import AuthContext

from .conftest import IDENTITY, Harness
from .test_workflows import PAYLOAD, put


def invoke(
    harness: Harness, method: str, path: str, body: dict[str, object] | None = None, **extra: object
) -> tuple[int, dict[str, object], dict[str, object]]:
    event: dict[str, object] = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        **extra,
    }
    response = harness.app.handle(event)
    return response["statusCode"], json.loads(response["body"]), response


def test_all_eight_api_routes_end_to_end(harness: Harness) -> None:
    status, body, response = invoke(harness, "GET", "/session/context")
    assert status == 200 and body["userId"] == IDENTITY.userId
    assert body["allowedOrganizations"] == [o.model_dump() for o in IDENTITY.allowedOrganizations]
    assert response["headers"]["X-Request-Id"] == body["requestId"]
    harness.register()
    correlation = str(uuid4())
    request = {
        "operationKey": str(uuid4()),
        "organizationId": "org-demo",
        "participantCode": "00001",
        "insuranceYear": 2026,
        "businessType": "DEMO",
    }
    status, body, _ = invoke(
        harness, "POST", "/acquisitions", request, headers={"x-request-id": correlation}
    )
    assert status == 202 and body["requestId"] == correlation
    acquisition_id = UUID(body["acquisitionId"])
    status, body, _ = invoke(harness, "GET", f"/acquisitions/{acquisition_id}")
    assert status == 200 and body["status"] == "QUEUED" and "files" not in body
    harness.worker.run(acquisition_id, UUID(correlation))
    status, body, _ = invoke(harness, "GET", f"/acquisitions/{acquisition_id}")
    assert status == 200 and body["status"] == "READY"
    status, download, _ = invoke(
        harness,
        "POST",
        f"/acquisitions/{acquisition_id}/download-url",
        {"fileId": body["files"][0]["fileId"]},
    )
    assert status == 200 and download["method"] == "GET" and download["expiresAt"]
    status, body, _ = invoke(
        harness,
        "POST",
        "/transfers",
        {
            "operationKey": str(uuid4()),
            "acquisitionId": str(acquisition_id),
            "outputType": "DEMO_OUTPUT",
            "layoutVersion": "demo-v1",
            "contentLength": len(PAYLOAD),
            "contentDigest": hashlib.sha256(PAYLOAD).hexdigest(),
        },
    )
    assert status == 201 and body["status"] == "PREPARED" and body["completedAt"] is None
    transfer_id = UUID(body["transferId"])
    status, body, _ = invoke(harness, "POST", f"/transfers/{transfer_id}/upload-url")
    assert status == 200 and body["requiredHeaders"]
    put(harness, transfer_id)
    status, completed, _ = invoke(harness, "POST", f"/transfers/{transfer_id}/complete", {})
    assert status == 200 and completed["status"] == "TRANSFERRED"
    status, body, _ = invoke(harness, "GET", f"/transfers/{transfer_id}")
    assert status == 200 and body["completedAt"] == completed["completedAt"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("participantCode", 1),
        ("participantCode", ""),
        ("insuranceYear", "2026"),
        ("insuranceYear", True),
        ("operationKey", "invalid"),
        ("organizationId", None),
        ("extra", "forbidden"),
    ],
)
def test_strict_request_validation(harness: Harness, field: str, value: object) -> None:
    request = {
        "operationKey": str(uuid4()),
        "organizationId": "org-demo",
        "participantCode": "00001",
        "insuranceYear": 2026,
        "businessType": "DEMO",
    }
    request[field] = value
    status, body, _ = invoke(harness, "POST", "/acquisitions", request)
    assert status == 400 and body["error"]["code"] == "E1001" and body["error"]["details"]


@pytest.mark.parametrize("raw", [None, "", "{", "null", "[]", 42, {}])
def test_bad_body_is_validation_error(harness: Harness, raw: object) -> None:
    result = harness.app.handle({"httpMethod": "POST", "path": "/acquisitions", "body": raw})
    assert result["statusCode"] == 400


@pytest.mark.parametrize(
    "extra",
    [
        {"body": "{}"},
        {"queryStringParameters": {"organizationId": "all"}},
        {"rawQueryString": "a=1"},
    ],
)
def test_session_rejects_body_and_query(harness: Harness, extra: dict[str, object]) -> None:
    status, _, _ = invoke(harness, "GET", "/session/context", **extra)
    assert status == 400


def test_empty_organizations_does_not_grant_access(harness: Harness) -> None:
    harness.auth.context = AuthContext(userId=IDENTITY.userId)
    status, body, _ = invoke(harness, "GET", "/session/context")
    assert status == 200 and body["allowedOrganizations"] == []
    status, _, _ = invoke(
        harness,
        "POST",
        "/acquisitions",
        {
            "operationKey": str(uuid4()),
            "organizationId": "org-demo",
            "participantCode": "00001",
            "insuranceYear": 2026,
            "businessType": "DEMO",
        },
    )
    assert status == 403


def test_gateway_authentication_and_missing_mapping(harness: Harness) -> None:
    harness.app.authenticator = GatewayAuthenticator()
    assert invoke(harness, "GET", "/session/context")[0] == 401
    context = {"authorizer": {"identity": json.dumps({"userId": "ocr-user"})}}
    status, body, _ = invoke(harness, "GET", "/session/context", requestContext=context)
    assert status == 200 and body["allowedOrganizations"] == []
    assert (
        invoke(
            harness,
            "GET",
            "/session/context",
            headers={"Authorization": "secret", "X-User-Id": "ocr-user"},
        )[0]
        == 401
    )


def test_bad_request_id_is_replaced_and_internal_errors_are_masked(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("password=private token=secret")

    monkeypatch.setattr(harness.service, "get_acquisition", fail)
    with caplog.at_level("INFO", logger="workflow"):
        status, body, response = invoke(
            harness,
            "GET",
            f"/acquisitions/{uuid4()}",
            headers={"X-Request-Id": "wrong", "Authorization": "credential-secret"},
        )
    assert status == 500 and body["error"]["code"] == "E5000"
    UUID(body["requestId"])
    assert (
        "secret" not in json.dumps(response)
        and "private" not in caplog.text
        and "credential-secret" not in caplog.text
    )


def test_gateway_v2_and_unknown_route(harness: Harness) -> None:
    event = {"rawPath": "/session/context", "requestContext": {"http": {"method": "GET"}}}
    assert harness.app.handle(event)["statusCode"] == 200
    assert invoke(harness, "POST", "/auth/login")[0] == 404
    assert invoke(harness, "GET", "/acquisitions/not-a-uuid")[0] == 400
    assert invoke(harness, "GET", "/acquisitions/" + str(uuid4()))[0] == 404
