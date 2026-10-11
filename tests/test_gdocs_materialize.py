"""Materialization tests for Google Docs exports (no network).

The image used for verification only has ``pytest`` installed (not
``pytest-asyncio``), so the async code is driven with ``asyncio.run`` inside
sync tests.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from docling_serve.gdocs import (
    GdocError,
    GdocRef,
    fetch_export_bytes,
    google_access_token,
    is_gdoc_export_url,
    materialize_gdoc,
    normalize_gdoc_sources,
    parse_gdoc_url,
)
from docling_serve.settings import DoclingServeSettings

DOC_ID = "1aBcDeFgHiJkLmNoPqRsTuVw"
REF = GdocRef(kind="document", doc_id=DOC_ID)
KEYED_REF = GdocRef(kind="document", doc_id=DOC_ID, resource_key="0-AbCdEfGhIjKl")
ARXIV_URL = "https://arxiv.org/pdf/2408.09869"

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _materialize(handler: Handler, **kwargs: Any):
    async with _mock_client(handler) as client:
        return await materialize_gdoc(REF, client=client, **kwargs)


def test_lowercase_content_disposition_filename_is_honoured():
    """D1 regression: requests lowercases headers, so the lookup must too."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-disposition": 'attachment; filename="Chapter 2 Report.docx"'
            },
            content=b"PK\x03\x04docx-bytes",
        )

    result = _run(_materialize(handler))

    assert result.kind == "file"
    assert result.filename == "Chapter 2 Report.docx"
    assert base64.b64decode(result.base64_string) == b"PK\x03\x04docx-bytes"


def test_missing_content_disposition_falls_back_to_doc_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"docx-bytes")

    result = _run(_materialize(handler))

    assert result.filename == f"{DOC_ID}.docx"


def test_rfc5987_filename_is_percent_decoded():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-disposition": (
                    "attachment; filename*=UTF-8''Chapter%202%20Report.docx"
                )
            },
            content=b"docx-bytes",
        )

    result = _run(_materialize(handler))

    assert result.filename == "Chapter 2 Report.docx"


def test_gone_response_reports_sharing_problem():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    with pytest.raises(GdocError, match="Anyone with the link"):
        _run(_materialize(handler))


def test_html_signin_interstitial_reports_sharing_problem():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html>Sign in</html>",
        )

    with pytest.raises(GdocError, match="Anyone with the link"):
        _run(_materialize(handler))


def test_body_larger_than_limit_is_rejected():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 256)

    with pytest.raises(GdocError, match="256"):
        _run(_materialize(handler, max_file_size=64))


def test_transport_error_is_reported():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(GdocError, match="Could not reach Google"):
        _run(_materialize(handler))


def test_normalize_leaves_non_google_source_untouched():
    from docling.datamodel.service.requests import ConvertSourcesRequest

    assert parse_gdoc_url(ARXIV_URL) is None

    request = ConvertSourcesRequest(sources=[{"kind": "http", "url": ARXIV_URL}])
    result = _run(normalize_gdoc_sources(request))

    assert len(result.sources) == 1
    assert result.sources[0] == request.sources[0]
    assert result.sources[0].kind == "http"


def test_normalize_replaces_google_source_with_file():
    from docling.datamodel.service.requests import ConvertSourcesRequest

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"docx-bytes")

    async def scenario():
        request = ConvertSourcesRequest(
            sources=[
                {
                    "kind": "http",
                    "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                }
            ]
        )
        async with _mock_client(handler) as client:
            return await normalize_gdoc_sources(request, client=client)

    result = _run(scenario())

    assert len(result.sources) == 1
    assert result.sources[0].kind == "file"
    assert result.sources[0].filename == f"{DOC_ID}.docx"


def test_resource_key_retry_without_key_wins_and_honours_filename():
    """A keyed link denied once is retried without the key and can succeed."""
    queries: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query)
        if b"resourcekey" in request.url.query:
            return httpx.Response(410)
        return httpx.Response(
            200,
            headers={
                "content-disposition": 'attachment; filename="Chapter 2 Report.docx"'
            },
            content=b"PK\x03\x04docx-bytes",
        )

    async def scenario():
        async with _mock_client(handler) as client:
            return await fetch_export_bytes(KEYED_REF, client=client)

    content, filename = _run(scenario())

    assert content == b"PK\x03\x04docx-bytes"
    assert filename == "Chapter 2 Report.docx"
    assert len(queries) == 2
    assert b"resourcekey" in queries[0]
    assert b"resourcekey" not in queries[1]


def test_resource_key_retry_exhausted_reports_sharing_problem():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(410)

    async def scenario():
        async with _mock_client(handler) as client:
            return await materialize_gdoc(KEYED_REF, client=client)

    with pytest.raises(GdocError, match="Anyone with the link"):
        _run(scenario())

    assert attempts == 2


def test_already_exported_source_is_passed_through_untouched():
    """An export URL is resolved; re-fetching it would drop the caller's headers."""
    from docling.datamodel.service.requests import ConvertSourcesRequest

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"docx-bytes")

    export_url = (
        f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx"
    )
    request = ConvertSourcesRequest(
        sources=[
            {
                "kind": "http",
                "url": export_url,
                "headers": {"Authorization": "Bearer caller-token"},
            }
        ]
    )

    async def scenario():
        async with _mock_client(handler) as client:
            return await normalize_gdoc_sources(request, client=client)

    result = _run(scenario())

    assert calls == []
    assert len(result.sources) == 1
    source = result.sources[0]
    assert str(source.url) == export_url
    assert source.headers == {"Authorization": "Bearer caller-token"}


def test_caller_headers_are_forwarded_to_the_export_fetch():
    from docling.datamodel.service.requests import ConvertSourcesRequest

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-disposition": 'attachment; filename="Doc.docx"'},
            content=b"docx-bytes",
        )

    request = ConvertSourcesRequest(
        sources=[
            {
                "kind": "http",
                "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                "headers": {"Authorization": "Bearer caller-token"},
            }
        ]
    )

    async def scenario():
        async with _mock_client(handler) as client:
            return await normalize_gdoc_sources(request, client=client)

    result = _run(scenario())

    assert len(seen) == 1
    assert seen[0].headers.get("authorization") == "Bearer caller-token"
    assert result.sources[0].kind == "file"
    assert result.sources[0].filename == "Doc.docx"


def test_drive_download_url_is_passed_through_untouched():
    from docling.datamodel.service.requests import ConvertSourcesRequest

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"bytes")

    download_url = f"https://drive.google.com/uc?export=download&id={DOC_ID}"
    request = ConvertSourcesRequest(sources=[{"kind": "http", "url": download_url}])

    async def scenario():
        async with _mock_client(handler) as client:
            return await normalize_gdoc_sources(request, client=client)

    result = _run(scenario())

    assert calls == []
    assert str(result.sources[0].url) == download_url


def _uncredentialed_settings() -> DoclingServeSettings:
    """Settings with explicit empties, so an ambient .env cannot leak in."""
    return DoclingServeSettings(
        gdoc_client_id="",
        gdoc_client_secret="",
        gdoc_refresh_token="",
    )


def _credentialed_settings() -> DoclingServeSettings:
    return DoclingServeSettings(
        gdoc_client_id="test-client-id",
        gdoc_client_secret="test-client-secret",
        gdoc_refresh_token="test-refresh-token",
    )


IS_EXPORT_URL_CASES = [
    (f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx", True),
    (f"https://docs.google.com/spreadsheets/d/{DOC_ID}/export?format=xlsx", True),
    (f"https://drive.google.com/uc?export=download&id={DOC_ID}", True),
    ("https://drive.usercontent.google.com/download?id=abc", True),
    (f"https://docs.google.com/document/d/{DOC_ID}/edit", False),
    (f"https://docs.google.com/document/d/{DOC_ID}/view", False),
    (f"https://drive.google.com/file/d/{DOC_ID}/view", False),
    ("https://example.com/document/d/" + DOC_ID + "/export", False),
    ("https://arxiv.org/pdf/2408.09869", False),
]


@pytest.mark.parametrize(("url", "expected"), IS_EXPORT_URL_CASES)
def test_is_gdoc_export_url(url: str, expected: bool):
    assert is_gdoc_export_url(url) is expected


def test_google_access_token_is_none_when_unconfigured():
    assert google_access_token(_uncredentialed_settings()) is None


def test_google_access_token_refresh_failure_names_the_settings(monkeypatch):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import credentials as google_credentials

    import docling_serve.gdocs as gdocs_module

    class _FailingCredentials:
        token = None
        expired = True

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def refresh(self, request: object) -> None:
            raise RuntimeError("refresh refused")

    class _Request:
        pass

    monkeypatch.setattr(gdocs_module, "_credentials", None)
    monkeypatch.setattr(google_credentials, "Credentials", _FailingCredentials)
    monkeypatch.setattr(google_requests, "Request", _Request)

    with pytest.raises(GdocError, match="DOCLING_SERVE_GDOC_CLIENT_ID"):
        google_access_token(_credentialed_settings())


def test_unconfigured_credentials_send_no_authorization_header():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"docx-bytes")

    async def scenario():
        async with _mock_client(handler) as client:
            return await fetch_export_bytes(
                REF, client=client, settings=_uncredentialed_settings()
            )

    _run(scenario())

    assert len(seen) == 1
    assert "authorization" not in seen[0].headers


def test_configured_credentials_add_a_bearer_token(monkeypatch):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"docx-bytes")

    monkeypatch.setattr(
        "docling_serve.gdocs.google_access_token", lambda settings: "tok"
    )

    async def scenario():
        async with _mock_client(handler) as client:
            return await fetch_export_bytes(
                REF, client=client, settings=_credentialed_settings()
            )

    _run(scenario())

    assert len(seen) == 1
    assert seen[0].headers.get("authorization") == "Bearer tok"


def test_retry_does_not_add_an_uncredentialed_attempt_when_unconfigured():
    attempts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) >= 3:
            # A credentialless third attempt would succeed; it must never happen.
            return httpx.Response(200, content=b"docx-bytes")
        return httpx.Response(410)

    async def scenario():
        async with _mock_client(handler) as client:
            with pytest.raises(GdocError, match="Anyone with the link"):
                await fetch_export_bytes(
                    KEYED_REF, client=client, settings=_uncredentialed_settings()
                )

    _run(scenario())

    assert len(attempts) == 2
    assert b"resourcekey" in attempts[0].url.query
    assert b"resourcekey" not in attempts[1].url.query


def test_broken_credentials_still_fetch_a_public_document(monkeypatch):
    attempts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if "authorization" in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, content=b"docx-bytes")

    monkeypatch.setattr(
        "docling_serve.gdocs.google_access_token", lambda settings: "tok"
    )

    async def scenario():
        async with _mock_client(handler) as client:
            return await fetch_export_bytes(
                KEYED_REF, client=client, settings=_credentialed_settings()
            )

    content, _ = _run(scenario())

    assert content == b"docx-bytes"
    assert len(attempts) == 3
    assert b"resourcekey" in attempts[0].url.query
    assert b"resourcekey" not in attempts[1].url.query
    assert "authorization" not in attempts[2].headers


def test_not_accessible_message_gains_credentials_sentence_only_when_configured(
    monkeypatch,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    async def scenario(settings: DoclingServeSettings) -> None:
        async with _mock_client(handler) as client:
            await fetch_export_bytes(REF, client=client, settings=settings)

    with pytest.raises(GdocError) as plain:
        _run(scenario(_uncredentialed_settings()))
    assert "configured Google credentials" not in str(plain.value)

    monkeypatch.setattr(
        "docling_serve.gdocs.google_access_token", lambda settings: "tok"
    )
    with pytest.raises(GdocError) as credentialed:
        _run(scenario(_credentialed_settings()))
    assert "configured Google credentials" in str(credentialed.value)

