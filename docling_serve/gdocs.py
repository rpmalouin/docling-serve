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
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

    from docling.datamodel.service.requests import FileSourceRequest

    from docling_serve.settings import DoclingServeSettings

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

# "open?id=<id>" links (docs.google.com/open?id=..., docs.google.com/a/<domain>/open?id=...,
# drive.google.com/open?id=...) are what Google Drive shortcut (.gdoc/.gsheet/.gslides)
# files carry. The URL alone does not name the file type, so it returns "document" and
# the caller corrects the kind from a stub extension or a /d/<kind>/ path.
_OPEN_PATH_RE = re.compile(r"^(?:a/[^/]+/)?open/?$")

# Shortcut file extension -> export kind. These files are ~170-byte JSON pointers,
# not documents, so the serve layer resolves them before handing work to the engine.
_STUB_EXT_KIND: dict[str, str] = {
    ".gdoc": "document",
    ".gsheet": "spreadsheet",
    ".gslides": "presentation",
}

# Other Google Drive shortcut extensions we recognize but cannot convert, mapped to
# the product name used in the user-facing error.
_UNSUPPORTED_STUB_EXT: dict[str, str] = {
    ".gform": "a Google Forms shortcut",
    ".gdraw": "a Google Drawings shortcut",
    ".gscript": "a Google Apps Script shortcut",
    ".gjam": "a Google Jamboard shortcut",
    ".gtable": "a Google Table shortcut",
}

_EXPORT_EXT: dict[str, str] = {
    "document": "docx",
    "spreadsheet": "xlsx",
    "presentation": "pptx",
}

_GDOC_EXPORT_BASE = "https://docs.google.com"


class GdocError(Exception):
    """A Google link we recognized but cannot convert. str(exc) is user-facing."""


class _AccessError(GdocError):
    """Internal: the export was denied with a retryable access status.

    Google's export endpoint is inconsistent about resource keys: a keyed link can
    return 401/403/404/410 even when the same link works without the key. This
    subtype lets :func:`fetch_export_bytes` tell that case apart from other failures
    so it can retry once without the key.
    """


#: OAuth token endpoint used to refresh the configured Google credentials.
_GDOC_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Module-level cache: one refreshed Google credentials object serves every
# conversion for the life of the process, instead of refreshing per request.
_credentials: Credentials | None = None


def google_access_token(settings: DoclingServeSettings) -> str | None:
    """Return a bearer token for the configured Google credentials, or None when unconfigured.

    The credentials are refreshed lazily and cached in a module global so an hour
    of conversions reuses one access token. The configured secret values are never
    logged; only whether credentials are present.
    """
    global _credentials

    client_id = settings.gdoc_client_id
    client_secret = settings.gdoc_client_secret.get_secret_value()
    refresh_token = settings.gdoc_refresh_token.get_secret_value()
    if not (client_id and client_secret and refresh_token):
        _log.debug("Google credentials are not configured (credentials=no)")
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if _credentials is None:
        _credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_GDOC_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
        )

    if _credentials.expired or not _credentials.token:
        try:
            _credentials.refresh(Request())
        except Exception as exc:
            _credentials = None
            raise GdocError(
                "The configured Google credentials could not be refreshed. Check "
                "the DOCLING_SERVE_GDOC_CLIENT_ID, "
                "DOCLING_SERVE_GDOC_CLIENT_SECRET and "
                "DOCLING_SERVE_GDOC_REFRESH_TOKEN settings."
            ) from exc
        _log.info("Refreshed the configured Google credentials (credentials=yes)")

    return _credentials.token


@dataclass(frozen=True, slots=True)
class GdocRef:
    """A parsed reference to a single Google document, sheet, slide or file."""

    kind: str  # "document" | "spreadsheet" | "presentation" | "file"
    doc_id: str
    resource_key: str | None = None

    def export_url(self) -> str:
        """Return Google's binary export/download URL for this reference.

        Mirrors the mapping used by ``docling-core`` so the serve layer and the
        engine agree on which endpoint to fetch.
        """
        if self.kind == "file":
            url = f"https://drive.google.com/uc?export=download&id={self.doc_id}"
        elif self.kind == "document":
            url = f"{_GDOC_EXPORT_BASE}/document/d/{self.doc_id}/export?format=docx"
        elif self.kind == "spreadsheet":
            url = f"{_GDOC_EXPORT_BASE}/spreadsheets/d/{self.doc_id}/export?format=xlsx"
        elif self.kind == "presentation":
            url = f"{_GDOC_EXPORT_BASE}/presentation/d/{self.doc_id}/export?format=pptx"
        else:
            raise GdocError(f"Unsupported Google document kind: {self.kind!r}")
        if self.resource_key:
            url = f"{url}&resourcekey={self.resource_key}"
        return url


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

    if _OPEN_PATH_RE.match(path.lstrip("/")):
        # The URL alone does not name the file type (open?id= works for Docs,
        # Sheets and Slides alike), so assume "document"; callers correct the
        # kind from a stub extension or a /d/<kind>/ path when available.
        ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        doc_id = ids[0] if ids else ""
        if not _ID_RE.match(doc_id):
            raise GdocError(
                f"Malformed Google Docs link: could not read a valid document id "
                f"({doc_id!r}). Paste the document's normal share link."
            )
        return GdocRef(kind="document", doc_id=doc_id)

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


def _stub_resource_key(data: dict[str, object]) -> str | None:
    """Return the first usable resource key carried by a shortcut pointer."""
    for key in ("resource_key", "resourcekey", "resourceKey"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_gdoc_stub(filename: str, content: bytes) -> GdocRef | None:
    """Resolve an uploaded Google Drive shortcut file to a :class:`GdocRef`.

    Google Drive stores ``.gdoc``/``.gsheet``/``.gslides`` files as a small JSON
    pointer (``{"url": "https://docs.google.com/open?id=<id>", "doc_id": ...}``)
    rather than a document, which the engine cannot parse. This reads that stub
    and returns the real reference so the caller can fetch the export instead.

    Returns ``None`` for a filename that is not a Google shortcut, so a normal
    upload passes through untouched. Raises :class:`GdocError` for a recognized
    shortcut that cannot be resolved or converted.
    """
    suffix = Path(filename).suffix.lower()
    unsupported = _UNSUPPORTED_STUB_EXT.get(suffix)
    if unsupported is not None:
        raise GdocError(
            f"{filename!r} is {unsupported}, which cannot be converted. Export it "
            "from Google Drive as a supported file type and upload that."
        )

    ext_kind = _STUB_EXT_KIND.get(suffix)
    if ext_kind is None:
        return None

    def _bad_stub() -> GdocError:
        return GdocError(
            f"{filename!r} does not look like a Google Drive shortcut: expected a "
            "JSON pointer with a document id (got malformed or incomplete data)."
        )

    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise _bad_stub() from exc

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _bad_stub() from exc
    if not isinstance(data, dict):
        raise _bad_stub()

    resource_key = _stub_resource_key(data)

    kind = ext_kind
    doc_id = data.get("doc_id")
    url_ref: GdocRef | None = None
    url = data.get("url")
    if isinstance(url, str) and url:
        try:
            url_ref = parse_gdoc_url(url)
        except GdocError as exc:
            if not (isinstance(doc_id, str) and doc_id):
                raise GdocError(
                    f"{filename!r} does not look like a Google Drive shortcut: {exc}"
                ) from exc
            url_ref = None
        if url_ref is not None:
            path = urlsplit(url).path.lstrip("/")
            if _PATH_RE.match(path):
                # A real /d/<kind>/ link names the type, so it wins over the
                # shortcut's extension; the bare open?id= form does not.
                kind = url_ref.kind

    if not (isinstance(doc_id, str) and doc_id):
        if url_ref is None:
            raise _bad_stub()
        doc_id = url_ref.doc_id

    return GdocRef(kind=kind, doc_id=doc_id, resource_key=resource_key)


def _not_accessible_message(doc_id: str, *, credentials_configured: bool = False) -> str:
    message = (
        f"Google document {doc_id} is not accessible without signing in. "
        "Share it with 'Anyone with the link → Viewer' and retry."
    )
    if credentials_configured:
        message += (
            " The configured Google credentials also failed to open it; check that "
            "the document is accessible to that Google account."
        )
    return message


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


async def _fetch_export_once(
    ref: GdocRef,
    client: httpx.AsyncClient,
    *,
    max_file_size: int | None,
    headers: Mapping[str, str] | None = None,
    credentials_configured: bool = False,
) -> tuple[bytes, str]:
    """Fetch one Google export attempt, returning ``(content, filename)``."""
    url = ref.export_url()
    _log.info("Materializing Google %s export for id=%s", ref.kind, ref.doc_id)
    try:
        async with client.stream("GET", url, headers=dict(headers or {})) as response:
            status = response.status_code
            if status in (401, 403, 404, 410):
                raise _AccessError(
                    _not_accessible_message(
                        ref.doc_id, credentials_configured=credentials_configured
                    )
                )
            if status >= 400:
                raise GdocError(
                    f"Google returned HTTP {status} when exporting "
                    f"document {ref.doc_id}."
                )

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type.lower():
                # Google served its sign-in interstitial, not a document.
                raise GdocError(
                    _not_accessible_message(
                        ref.doc_id, credentials_configured=credentials_configured
                    )
                )

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
                    raise GdocError(_too_large_message(ref, received, max_file_size))
                chunks.append(chunk)

            content = b"".join(chunks)
            filename = _resolve_filename(ref, response.headers)
    except GdocError:
        raise
    except httpx.HTTPError as exc:
        raise GdocError(
            f"Could not reach Google to export document {ref.doc_id}: {exc}"
        ) from exc

    _log.info(
        "Materialized Google export id=%s as %r (%d bytes)",
        ref.doc_id,
        filename,
        len(content),
    )
    return content, filename


async def fetch_export_bytes(
    ref: GdocRef,
    *,
    max_file_size: int | None = None,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
    headers: Mapping[str, str] | None = None,
    settings: DoclingServeSettings | None = None,
) -> tuple[bytes, str]:
    """Fetch a Google export, returning ``(content, filename)``.

    Shared by the HTTP-source path (:func:`materialize_gdoc`) and the uploaded
    Google shortcut path in the API layer. ``headers`` are the caller's own
    request headers (e.g. an ``Authorization`` bearer token from an http source);
    they win over the configured credentials and are sent on every attempt. When
    no ``Authorization`` header is supplied and the optional
    ``DOCLING_SERVE_GDOC_*`` credentials are configured, a refreshed bearer token
    is added.

    Each attempt is retried in order, stopping at the first success: with the
    resource key (if any), then without it (Google's export endpoint is
    inconsistent about keyed links), then once more without the credential-derived
    ``Authorization`` header so a broken credential cannot block a public document.

    Raises:
        GdocError: The export could not be fetched or is not a binary document.
    """
    from docling_serve import settings as settings_module

    resolved_settings = settings or settings_module.docling_serve_settings

    request_headers = dict(headers or {})
    has_caller_authorization = any(
        key.lower() == "authorization" for key in request_headers
    )
    credentials_used = False
    if not has_caller_authorization:
        token = google_access_token(resolved_settings)
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
            credentials_used = True

    _log.info(
        "Fetching Google %s export for id=%s credentials=%s",
        ref.kind,
        ref.doc_id,
        "yes" if credentials_used else "no",
    )

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)

    async def _attempt(
        target: GdocRef, attempt_headers: Mapping[str, str]
    ) -> tuple[bytes, str]:
        return await _fetch_export_once(
            target,
            client,
            max_file_size=max_file_size,
            headers=attempt_headers,
            credentials_configured=credentials_used,
        )

    try:
        try:
            return await _attempt(ref, request_headers)
        except _AccessError:
            pass

        bare = ref
        if ref.resource_key is not None:
            _log.info(
                "Retrying Google %s export for id=%s without resource key",
                ref.kind,
                ref.doc_id,
            )
            bare = GdocRef(kind=ref.kind, doc_id=ref.doc_id)
            try:
                return await _attempt(bare, request_headers)
            except _AccessError:
                pass

        if credentials_used:
            _log.info(
                "Retrying Google %s export for id=%s without credentials",
                ref.kind,
                ref.doc_id,
            )
            try:
                return await _attempt(bare, dict(headers or {}))
            except _AccessError:
                pass

        raise _AccessError(
            _not_accessible_message(ref.doc_id, credentials_configured=credentials_used)
        )
    finally:
        if owns_client:
            await client.aclose()


async def materialize_gdoc(
    ref: GdocRef,
    *,
    max_file_size: int | None = None,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
    headers: Mapping[str, str] | None = None,
    settings: DoclingServeSettings | None = None,
) -> FileSourceRequest:
    """Fetch a Google export and wrap it as a ``FileSourceRequest``.

    Args:
        ref: The parsed Google reference to export.
        max_file_size: Optional size limit, in bytes, enforced from
            ``Content-Length`` and while streaming.
        timeout: Request timeout, in seconds, used when creating a client.
        client: Optional pre-built client (tests inject a mock transport). When
            omitted, a client is created and closed here.
        headers: Optional caller headers to send with the export request.
        settings: Optional settings; defaults to the process settings. Only the
            optional Google credentials are read from it.

    Raises:
        GdocError: The export could not be fetched or is not a binary document.
    """
    from docling.datamodel.service.requests import FileSourceRequest

    content, filename = await fetch_export_bytes(
        ref,
        max_file_size=max_file_size,
        timeout=timeout,
        client=client,
        headers=headers,
        settings=settings,
    )
    return FileSourceRequest(
        base64_string=base64.b64encode(content).decode("ascii"),
        filename=filename,
    )


_ConvertRequestT = TypeVar("_ConvertRequestT", bound=BaseModel)

# Hosts Google redirects an export/download to. A URL already on one of these —
# or an explicit export path — is resolved; re-materializing it would discard the
# caller's own headers, so it is passed through untouched.
_EXPORT_HOSTS = frozenset({"drive.usercontent.google.com"})


def is_gdoc_export_url(url: str) -> bool:
    """Return whether ``url`` already points at Google's export/download endpoint."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    path = parsed.path
    if host in _EXPORT_HOSTS:
        return True
    if host == "docs.google.com":
        return "/export" in path and _PATH_RE.match(path.lstrip("/")) is not None
    if host == "drive.google.com":
        return path.startswith("/uc") and "export=download" in parsed.query.lower()
    return False


async def normalize_gdoc_sources(
    request: _ConvertRequestT,
    *,
    max_file_size: int | None = None,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> _ConvertRequestT:
    """Replace HTTP sources that are Google links with materialized exports.

    Sources that are not recognized Google links are left untouched, so a plain
    ``http`` source keeps flowing through the normal engine fetch path. A source
    that already points at Google's export endpoint is left untouched too: it is
    resolved, and re-fetching it here would drop the caller's own headers (an
    ``Authorization`` bearer token, for instance).
    """
    new_sources = []
    for source in request.sources:  # type: ignore[attr-defined]
        if getattr(source, "kind", None) == "http":
            url = getattr(source, "url", None)
            if url is not None and not is_gdoc_export_url(str(url)):
                ref = parse_gdoc_url(str(url))
                if ref is not None:
                    new_sources.append(
                        await materialize_gdoc(
                            ref,
                            max_file_size=max_file_size,
                            timeout=timeout,
                            client=client,
                            headers=getattr(source, "headers", None),
                        )
                    )
                    continue
        new_sources.append(source)

    return request.model_copy(update={"sources": new_sources}, deep=True)  # type: ignore[attr-defined]
