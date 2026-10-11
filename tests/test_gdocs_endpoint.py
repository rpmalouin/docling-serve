"""Endpoint tests for Google Docs source handling."""

import base64

from fastapi.testclient import TestClient

from docling_serve.app import create_app
from docling_serve.orchestrator_factory import get_async_orchestrator
from docling_serve.settings import docling_serve_settings

DOC_ID = "1aBcDeFgHiJkLmNoPqRsTuVw"
PUB_ID = "2AbCdEfGhIjKlMnOpQrStUvW"
PUBLISHED_URL = f"https://docs.google.com/document/d/e/{PUB_ID}/pub"
GDOC_URL = f"https://docs.google.com/document/d/{DOC_ID}/edit"
EXPECTED_FILENAME = "Chapter 2 Report.docx"


def _auth_headers() -> dict:
    headers = {}
    if docling_serve_settings.api_key:
        headers["X-Api-Key"] = docling_serve_settings.api_key
    return headers


class _StubTask:
    task_id = "stub-task"
    task_type = "convert"
    task_status = "pending"
    processing_meta = None
    error_message = None
    failure = None


class _StubOrchestrator:
    """Records the sources the endpoint hands to the orchestrator."""

    def __init__(self) -> None:
        self.enqueued_sources = None

    async def enqueue(self, **kwargs):
        self.enqueued_sources = kwargs["sources"]
        return _StubTask()

    async def get_queue_position(self, task_id: str) -> int:
        return 0


def _app_with_stub(stub: _StubOrchestrator):
    app = create_app()
    # No lifespan is run: the stub keeps the app from building the real engine.
    app.dependency_overrides[get_async_orchestrator] = lambda: stub
    return app


def test_published_link_is_rejected_with_422():
    client = TestClient(_app_with_stub(_StubOrchestrator()))

    response = client.post(
        "/v1/convert/source",
        json={"sources": [{"kind": "http", "url": PUBLISHED_URL}]},
        headers=_auth_headers(),
    )

    assert response.status_code == 422
    assert "/d/<id>/edit" in response.json()["detail"]


def test_google_link_reaches_orchestrator_as_file(monkeypatch):
    stub = _StubOrchestrator()

    async def fake_materialize(ref, **kwargs):
        from docling.datamodel.service.requests import FileSourceRequest

        return FileSourceRequest(
            base64_string=base64.b64encode(b"docx-bytes").decode("ascii"),
            filename=EXPECTED_FILENAME,
        )

    monkeypatch.setattr("docling_serve.gdocs.materialize_gdoc", fake_materialize)
    client = TestClient(_app_with_stub(stub))

    response = client.post(
        "/v1/convert/source/async",
        json={
            "options": {"to_formats": ["json"]},
            "sources": [{"kind": "http", "url": GDOC_URL}],
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    sources = stub.enqueued_sources
    assert sources is not None and len(sources) == 1
    assert getattr(sources[0], "kind", None) == "file"
    assert getattr(sources[0], "filename", None) == EXPECTED_FILENAME
