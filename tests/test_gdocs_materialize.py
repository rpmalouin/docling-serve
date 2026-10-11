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
    materialize_gdoc,
    normalize_gdoc_sources,
    parse_gdoc_url,
)

DOC_ID = "1aBcDeFgHiJkLmNoPqRsTuVw"
REF = GdocRef(kind="document", doc_id=DOC_ID)
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
