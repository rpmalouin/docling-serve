"""Unit tests for Google Docs/Drive URL parsing (no network)."""

import pytest

from docling_serve.gdocs import GDOC_KINDS, GdocError, GdocRef, parse_gdoc_url

DOC_ID = "1aBcDeFgHiJkLmNoPqRsTuVw"
PUB_ID = "2AbCdEfGhIjKlMnOpQrStUvW"


def test_gdoc_kinds_is_the_supported_set():
    assert GDOC_KINDS == ("document", "spreadsheet", "presentation", "file")


ACCEPTED_URLS = [
    (f"https://docs.google.com/document/d/{DOC_ID}/edit", "document"),
    (f"https://docs.google.com/document/d/{DOC_ID}/view", "document"),
    (f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=sharing", "document"),
    (f"https://docs.google.com/document/u/0/d/{DOC_ID}/edit", "document"),
    (f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit#gid=0", "spreadsheet"),
    (
        f"https://docs.google.com/presentation/d/{DOC_ID}/edit?usp=sharing",
        "presentation",
    ),
    (f"https://drive.google.com/file/d/{DOC_ID}/view", "file"),
]


@pytest.mark.parametrize(("url", "kind"), ACCEPTED_URLS)
def test_parse_accepts_google_links(url: str, kind: str):
    ref = parse_gdoc_url(url)
    assert ref == GdocRef(kind=kind, doc_id=DOC_ID)


NON_GOOGLE_URLS = [
    "https://arxiv.org/pdf/2408.09869",
    "https://example.com/document/d/" + DOC_ID,
    "https://docs.example.com/document/d/" + DOC_ID,
]


@pytest.mark.parametrize("url", NON_GOOGLE_URLS)
def test_parse_returns_none_for_non_google_urls(url: str):
    assert parse_gdoc_url(url) is None


ERROR_URLS = [
    (
        f"https://docs.google.com/document/d/e/{PUB_ID}/pub",
        "published-to-web",
    ),
    (
        f"https://docs.google.com/document/d/{DOC_ID}/edit?resourcekey=0-abc",
        "resource-key",
    ),
    ("https://docs.google.com/document/d/short/edit", "Malformed"),
    ("https://docs.google.com/forms/d/" + DOC_ID + "/edit", "Unrecognized"),
]


@pytest.mark.parametrize(("url", "match"), ERROR_URLS)
def test_parse_rejects_unusable_google_links(url: str, match: str):
    with pytest.raises(GdocError, match=match):
        parse_gdoc_url(url)


EXPORT_URLS = [
    (
        GdocRef(kind="document", doc_id=DOC_ID),
        f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx",
    ),
    (
        GdocRef(kind="spreadsheet", doc_id=DOC_ID),
        f"https://docs.google.com/spreadsheets/d/{DOC_ID}/export?format=xlsx",
    ),
    (
        GdocRef(kind="presentation", doc_id=DOC_ID),
        f"https://docs.google.com/presentation/d/{DOC_ID}/export?format=pptx",
    ),
    (
        GdocRef(kind="file", doc_id=DOC_ID),
        f"https://drive.google.com/uc?export=download&id={DOC_ID}",
    ),
]


@pytest.mark.parametrize(("ref", "expected"), EXPORT_URLS)
def test_export_url_mirrors_docling_core(ref: GdocRef, expected: str):
    assert ref.export_url() == expected


def test_export_url_rejects_unknown_kind():
    with pytest.raises(GdocError):
        GdocRef(kind="slides", doc_id=DOC_ID).export_url()
