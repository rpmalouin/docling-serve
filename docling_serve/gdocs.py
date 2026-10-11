"""Serve-layer support for Google Docs/Sheets/Slides/Drive links.

Google Docs, Sheets and Slides are online-only formats that the conversion
engine cannot parse directly. This module recognizes a public Google link,
fetches Google's documented export endpoint (which returns a real OOXML file:
``docx``/``xlsx``/``pptx``, or the raw bytes for a Drive file), reads the real
filename from ``Content-Disposition`` case-insensitively, and hands the
orchestrator a :class:`FileSourceRequest` — the same ``file`` source kind the
API already accepts for uploads.

The wire format stays ``{"kind": "http", "url": ...}``: callers post a normal
HTTP source and :func:`normalize_gdoc_sources` transparently materializes the
export before request validation. Doing the export here (instead of relying on
the engine's URL rewrite) preserves the document's filename and turns access
failures into clear, user-facing 422 errors.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from docling.datamodel.service.requests import FileSourceRequest

_log = logging.getLogger(__name__)

#: Google path segment names that map to a supported export kind.
GDOC_KINDS: tuple[str, ...] = ("document", "spreadsheet", "presentation", "file")

_GDOC_HOSTS = frozenset({"docs.google.com", "drive.google.com"})

# Path segment -> public kind. Google uses the plural "spreadsheets" in URLs.
_PATH_KIND: dict[str, str] = {
    "document": "document",
    "spreadsheets": "spreadsheet",
    "presentation": "presentation",
    "file": "file",
}

# A Google document/file id: URL-safe base64-ish, at least 20 characters.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")

# Published-to-web documents use /<kind>/d/e/<pubid>/pub.
_PUBLISHED_RE = re.compile(r"(?:^|/)d/e/[A-Za-z0-9_-]+/pub(?:/|$)")

# Accepted path shapes, matched against the path with any leading "/" removed:
# an optional locale prefix, an optional leading "/u/<n>", an optional
# "drive/" prefix (drive.google.com/drive/...), the kind, an optional trailing
# "/u/<n>", then "/d/<id>".
_PATH_RE = re.compile(
    r"""
    ^
    (?:[a-z]{2}(?:[-_][a-z]{2})?/)?          # optional locale prefix
    (?:u/\d+/)?                               # optional leading /u/<n>
    (?:drive/)?                               # drive.google.com/drive/...
    (?P<kind>document|spreadsheets|presentation|file)
    (?:/u/\d+)?                               # optional /u/<n> after the kind
    /d/
    (?P<id>[^/]+)
    (?:/.*)?
    $
    """,
    re.VERBOSE,
)

_EXPORT_EXT: dict[str, str] = {
    "document": "docx",
    "spreadsheet": "xlsx",
    "presentation": "pptx",
}

_GDOC_EXPORT_BASE = "https://docs.google.com"


class GdocError(Exception):
    """A Google link we recognized but cannot convert. str(exc) is user-facing."""


@dataclass(frozen=True, slots=True)
class GdocRef:
    """A parsed reference to a single Google document, sheet, slide or file."""

    kind: str  # "document" | "spreadsheet" | "presentation" | "file"
    doc_id: str

    def export_url(self) -> str:
        """Return Google's binary export/download URL for this reference.

        Mirrors the mapping used by ``docling-core`` so the serve layer and the
        engine agree on which endpoint to fetch.
        """
        if self.kind == "file":
            return f"https://drive.google.com/uc?export=download&id={self.doc_id}"
        if self.kind == "document":
            return f"{_GDOC_EXPORT_BASE}/document/d/{self.doc_id}/export?format=docx"
        if self.kind == "spreadsheet":
            return (
                f"{_GDOC_EXPORT_BASE}/spreadsheets/d/{self.doc_id}/export?format=xlsx"
            )
        if self.kind == "presentation":
            return (
                f"{_GDOC_EXPORT_BASE}/presentation/d/{self.doc_id}/export?format=pptx"
            )
        raise GdocError(f"Unsupported Google document kind: {self.kind!r}")


def _has_resource_key(parsed: SplitResult) -> bool:
    """Return whether the URL carries a ``resourcekey`` query parameter."""
    for key in parse_qs(parsed.query, keep_blank_values=True):
        if key.lower() == "resourcekey":
            return True
    return "resourcekey=" in parsed.fragment.lower()


def _unrecognized_message(url: str) -> str:
    return (
        f"Unrecognized Google Docs/Drive link ({url!r}). Paste the document's "
        "share link, e.g. "
        "https://docs.google.com/document/d/<id>/edit."
    )


def parse_gdoc_url(url: str) -> GdocRef | None:
    """Parse a Google Docs/Sheets/Slides/Drive URL into a :class:`GdocRef`.

    Returns ``None`` when the URL is not a ``docs.google.com`` or
    ``drive.google.com`` URL at all. Raises :class:`GdocError` when the host is
    Google but the link cannot be converted (malformed id, published-to-web,
    resource-key protected, or an unknown path shape).
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host not in _GDOC_HOSTS:
        return None

    if _has_resource_key(parsed):
        raise GdocError(
            "This Google link is resource-key protected. Copy the plain share "
            "link (without the 'resourcekey' parameter) and retry."
        )

    path = parsed.path
    if _PUBLISHED_RE.search(path):
        raise GdocError(
            "This looks like a published-to-web Google link. Use the "
            "document's normal /d/<id>/edit share link instead."
        )

    match = _PATH_RE.match(path.lstrip("/"))
    if match is None:
        raise GdocError(_unrecognized_message(url))

    doc_id = match.group("id")
    if not _ID_RE.match(doc_id):
        raise GdocError(
            f"Malformed Google Docs link: could not read a valid document id "
            f"({doc_id!r}). Paste the document's normal share link."
        )

    return GdocRef(kind=_PATH_KIND[match.group("kind")], doc_id=doc_id)


def _not_accessible_message(doc_id: str) -> str:
    return (
        f"Google document {doc_id} is not accessible without signing in. "
        "Share it with 'Anyone with the link → Viewer' and retry."
    )


def _sanitize_filename(value: str) -> str | None:
    """Return a basename-safe filename, or ``None`` when nothing usable remains."""
    cleaned = value.strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
    return cleaned or None


def _parse_content_disposition(value: str) -> str | None:
    """Extract the filename from a ``Content-Disposition`` header value."""
    plain: str | None = None
    encoded: str | None = None
    for part in value.split(";"):
        token = part.strip()
        lowered = token.lower()
        if lowered.startswith("filename*="):
            encoded = token.split("=", 1)[1].strip()
        elif lowered.startswith("filename="):
            plain = token.split("=", 1)[1].strip().strip("'\"")

    if plain:
        return _sanitize_filename(plain)
    if encoded:
        raw = encoded
        if raw.count("'") >= 2:
            # RFC 5987: charset'language'percent-encoded-value
            raw = raw.split("'", 2)[2]
        return _sanitize_filename(unquote(raw))
    return None


def _fallback_filename(ref: GdocRef) -> str:
    """Build the fallback filename when the response carries no disposition."""
    ext = _EXPORT_EXT.get(ref.kind)
    if ext is not None:
        return f"{ref.doc_id}.{ext}"
    basename = Path(urlsplit(ref.export_url()).path).name
    return basename or ref.doc_id


def _resolve_filename(ref: GdocRef, headers: httpx.Headers) -> str:
    """Resolve the output filename, preferring ``Content-Disposition``.

    The header lookup is case-insensitive on purpose: ``requests`` (used by the
    engine) stores response headers lowercased, which is why the engine falls
    back to the URL basename and names every export "export".
    """
    for key, value in headers.items():
        if key.lower() == "content-disposition":
            filename = _parse_content_disposition(value)
            if filename:
                return filename
    return _fallback_filename(ref)


def _too_large_message(ref: GdocRef, size: int, limit: int) -> str:
    return (
        f"Google document {ref.doc_id} exceeds the maximum allowed size "
        f"({size} > {limit} bytes)."
    )


async def materialize_gdoc(
    ref: GdocRef,
    *,
    max_file_size: int | None = None,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> FileSourceRequest:
    """Fetch a Google export and wrap it as a ``FileSourceRequest``.

    Args:
        ref: The parsed Google reference to export.
        max_file_size: Optional size limit, in bytes, enforced from
            ``Content-Length`` and while streaming.
        timeout: Request timeout, in seconds, used when creating a client.
        client: Optional pre-built client (tests inject a mock transport). When
            omitted, a client is created and closed here.

    Raises:
        GdocError: The export could not be fetched or is not a binary document.
    """
    from docling.datamodel.service.requests import FileSourceRequest

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)

    url = ref.export_url()
    _log.info("Materializing Google %s export for id=%s", ref.kind, ref.doc_id)
    try:
        try:
            async with client.stream("GET", url) as response:
                status = response.status_code
                if status in (401, 403, 404, 410):
                    raise GdocError(_not_accessible_message(ref.doc_id))
                if status >= 400:
                    raise GdocError(
                        f"Google returned HTTP {status} when exporting "
                        f"document {ref.doc_id}."
                    )

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    # Google served its sign-in interstitial, not a document.
                    raise GdocError(_not_accessible_message(ref.doc_id))

                if max_file_size is not None:
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_size = int(declared)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > max_file_size:
                            raise GdocError(
                                _too_large_message(ref, declared_size, max_file_size)
                            )

                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if max_file_size is not None and received > max_file_size:
                        raise GdocError(
                            _too_large_message(ref, received, max_file_size)
                        )
                    chunks.append(chunk)

                content = b"".join(chunks)
                filename = _resolve_filename(ref, response.headers)
        except GdocError:
            raise
        except httpx.HTTPError as exc:
            raise GdocError(
                f"Could not reach Google to export document {ref.doc_id}: {exc}"
            ) from exc
    finally:
        if owns_client:
            await client.aclose()

    _log.info(
        "Materialized Google export id=%s as %r (%d bytes)",
        ref.doc_id,
        filename,
        len(content),
    )
    return FileSourceRequest(
        base64_string=base64.b64encode(content).decode("ascii"),
        filename=filename,
    )


_ConvertRequestT = TypeVar("_ConvertRequestT", bound=BaseModel)


async def normalize_gdoc_sources(
    request: _ConvertRequestT,
    *,
    max_file_size: int | None = None,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> _ConvertRequestT:
    """Replace HTTP sources that are Google links with materialized exports.

    Sources that are not recognized Google links are left untouched, so a plain
    ``http`` source keeps flowing through the normal engine fetch path.
    """
    new_sources = []
    for source in request.sources:  # type: ignore[attr-defined]
        if getattr(source, "kind", None) == "http":
            url = getattr(source, "url", None)
            if url is not None:
                ref = parse_gdoc_url(str(url))
                if ref is not None:
                    new_sources.append(
                        await materialize_gdoc(
                            ref,
                            max_file_size=max_file_size,
                            timeout=timeout,
                            client=client,
                        )
                    )
                    continue
        new_sources.append(source)

    return request.model_copy(update={"sources": new_sources}, deep=True)  # type: ignore[attr-defined]
