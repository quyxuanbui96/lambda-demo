"""Real localhost HTTP + Moto S3/SQS integration; no external AWS requests."""

import hashlib
import time
from threading import Thread
from uuid import uuid4

import pytest
import requests

from scripts.workflow_local import demo_server


@pytest.mark.integration
def test_eight_apis_over_http_and_direct_s3_upload() -> None:
    with demo_server(port=0, s3_port=0) as demo:
        server_thread = Thread(target=demo.server.serve_forever, daemon=True)
        server_thread.start()
        try:
            base = demo.endpoint
            response = requests.get(base + "/session/context", timeout=5)
            assert response.status_code == 200 and response.json()["userId"] == "demo-user"
            response = requests.post(
                base + "/acquisitions",
                json={
                    "operationKey": str(uuid4()),
                    "organizationId": "org-demo",
                    "participantCode": "00001",
                    "insuranceYear": 2026,
                    "businessType": "DEMO",
                },
                timeout=5,
            )
            assert response.status_code == 202
            acquisition = response.json()["acquisitionId"]
            for _ in range(40):
                response = requests.get(base + f"/acquisitions/{acquisition}", timeout=5)
                assert response.status_code == 200
                status = response.json()
                if status["status"] == "READY":
                    break
                assert status["status"] in {"QUEUED", "RUNNING"}, status
                time.sleep(0.1)
            assert status["status"] == "READY", status
            response = requests.post(
                base + f"/acquisitions/{acquisition}/download-url",
                json={"fileId": status["files"][0]["fileId"]},
                timeout=5,
            )
            assert response.status_code == 200
            download = requests.get(response.json()["downloadUrl"], timeout=5)
            assert (
                download.status_code == 200
                and b"Apple" in download.content
                and b"Excluded" not in download.content
            )
            data = download.content
            response = requests.post(
                base + "/transfers",
                json={
                    "operationKey": str(uuid4()),
                    "acquisitionId": acquisition,
                    "outputType": "DEMO_OUTPUT",
                    "layoutVersion": "demo-v1",
                    "contentLength": len(data),
                    "contentDigest": hashlib.sha256(data).hexdigest(),
                },
                timeout=5,
            )
            assert response.status_code == 201
            transfer = response.json()["transferId"]
            upload = requests.post(base + f"/transfers/{transfer}/upload-url", timeout=5)
            assert upload.status_code == 200
            target = upload.json()
            response = requests.put(
                target["uploadUrl"], headers=target["requiredHeaders"], data=data, timeout=5
            )
            assert response.status_code == 200
            completed = requests.post(base + f"/transfers/{transfer}/complete", timeout=5)
            assert completed.status_code == 200, completed.text
            assert completed.json()["status"] == "TRANSFERRED"
            result = requests.get(base + f"/transfers/{transfer}", timeout=5)
            assert (
                result.status_code == 200
                and result.json()["completedAt"] == completed.json()["completedAt"]
            )
        finally:
            demo.server.shutdown()
            server_thread.join(timeout=3)
