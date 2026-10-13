#!/usr/bin/env python3
"""Run the eight APIs locally with disposable SQLite and Moto S3/SQS HTTP servers."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
from botocore.config import Config
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from src.handlers.health import handler as health
from src.workflow.api import WorkflowApplication
from src.workflow.auth import DevelopmentAuthenticator
from src.workflow.config import WorkflowSettings
from src.workflow.contracts import ContractRegistry
from src.workflow.database import Database, metadata
from src.workflow.models import AuthContext, Organization, SourceVersion
from src.workflow.runtime import SqsDispatcher
from src.workflow.service import WorkflowService, utcnow
from src.workflow.storage import ObjectStore
from src.workflow.worker import AcquisitionWorker

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LocalServer:
    endpoint: str
    sqs: SQSClient
    queue_url: str
    stop: Event
    thread: Thread
    server: ThreadingHTTPServer


@contextmanager
def demo_server(port: int = 3000, s3_port: int = 5001) -> Iterator[LocalServer]:
    # Explicit localhost endpoints and fake credentials ignore developer AWS profiles.
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=s3_port, verbose=False)
    moto.start()
    _, actual_port = moto.get_host_and_port()
    endpoint = f"http://127.0.0.1:{actual_port}"
    region: Literal["ap-southeast-2"] = "ap-southeast-2"
    bucket = "workflow-local-demo"
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    sqs = boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    client.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    queue_url = sqs.create_queue(QueueName="local-acquisitions")["QueueUrl"]
    stop = Event()
    with TemporaryDirectory(prefix="workflow-local-") as temporary:
        database = Database.local(f"sqlite+pysqlite:///{temporary}/workflow.sqlite3")
        metadata.create_all(database.engine)
        identity = AuthContext(
            userId="demo-user",
            allowedOrganizations=[
                Organization(organizationId="org-demo", displayName="Demo organization")
            ],
        )
        settings = WorkflowSettings(
            stage="local",
            bucket=bucket,
            database_url=SecretStr("sqlite+pysqlite:///:memory:"),
            auth_mode="bypass",
            dev_identity=identity,
            contracts_path=ROOT / "examples/workflow/contracts.demo.json",
        )
        service = WorkflowService(
            database,
            ObjectStore(client, settings),
            ContractRegistry.load(settings.contracts_path),
            SqsDispatcher(sqs, queue_url),
            settings,
        )
        app, worker = WorkflowApplication(
            service, DevelopmentAuthenticator(identity)
        ), AcquisitionWorker(service)
        key = settings.source_prefix + "source.demo.csv"
        source = client.put_object(
            Bucket=bucket, Key=key, Body=(ROOT / "examples/workflow/source.demo.csv").read_bytes()
        )
        with database.transaction() as repo:
            repo.register_source(
                SourceVersion(
                    id=uuid4(),
                    organizationId="org-demo",
                    participantCode="00001",
                    insuranceYear=2026,
                    businessType="DEMO",
                    sourceSystem="OCR",
                    bucket=bucket,
                    key=key,
                    versionId=source["VersionId"],
                    layoutVersion="demo-source-v1",
                    capturedAt=utcnow(),
                )
            )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.handle_api()

            def do_POST(self) -> None:
                self.handle_api()

            def handle_api(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                if length > 64 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(length).decode("utf-8") if length else None
                event = {
                    "httpMethod": self.command,
                    "path": parts.path,
                    "body": body,
                    "rawQueryString": parts.query,
                    "headers": dict(self.headers),
                }
                result = (
                    health({}, None)
                    if parts.path == "/health" and self.command == "GET"
                    else app.handle(event)
                )
                self.send_response(int(str(result["statusCode"])))
                headers = result["headers"]
                assert isinstance(headers, dict)
                for header, value in headers.items():
                    self.send_header(header, str(value))
                data = str(result["body"]).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        def process_queue() -> None:
            while not stop.is_set():
                messages = sqs.receive_message(
                    QueueUrl=queue_url, WaitTimeSeconds=1, VisibilityTimeout=5
                ).get("Messages", [])
                for message in messages:
                    try:
                        body = json.loads(message["Body"])
                        worker.run(UUID(body["acquisitionId"]), UUID(body["requestId"]))
                        sqs.delete_message(
                            QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"]
                        )
                    except Exception:
                        logging.getLogger("workflow").warning("Local acquisition attempt failed")

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = Thread(target=process_queue, daemon=True)
        thread.start()
        try:
            yield LocalServer(
                f"http://127.0.0.1:{server.server_port}", sqs, queue_url, stop, thread, server
            )
        finally:
            stop.set()
            thread.join(timeout=3)
            server.server_close()
            database.engine.dispose()
            moto.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--s3-port", type=int, default=5001)
    args = parser.parse_args()
    with demo_server(args.port, args.s3_port) as local:
        print(
            f"Workflow demo: {local.endpoint}; org-demo / participant 00001 / year 2026 / DEMO",
            flush=True,
        )
        print("Auth bypass enabled for this local demo. Stop with Ctrl+C.", flush=True)
        with suppress(KeyboardInterrupt):
            local.server.serve_forever()


if __name__ == "__main__":
    main()
