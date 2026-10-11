"""Unit tests for Google Drive shortcut (.gdoc/.gsheet/.gslides) parsing.

Google Drive stores these uploads as a tiny JSON pointer rather than a document,
so the serve layer must resolve the pointer before the engine sees it.
"""

import json

import pytest

from docling_serve.gdocs import GdocError, GdocRef, parse_gdoc_stub

DOC_ID = "1aBcDeFgHiJkLmNoPqRsTuVw"
SHEET_ID = "2AbCdEfGhIjKlMnOpQrStUvW"
KEY = "0-AbCdEfGhIjKlMnOpQrStUvW"
OPEN_URL = f"https://docs.google.com/open?id={DOC_ID}"


def _stub(**fields: object) -> bytes:
    return json.dumps(fields).encode("utf-8")


def test_open_url_stub_resolves_to_document():
    ref = parse_gdoc_stub(
        "Acceptance Letter to Ed.gdoc",
        _stub(url=OPEN_URL, doc_id=DOC_ID),
    )

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)
    assert (
        ref.export_url()
        == f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx"
    )


def test_gsheet_extension_exports_xlsx():
    ref = parse_gdoc_stub(
        "Budget.gsheet",
        _stub(url=f"https://docs.google.com/open?id={SHEET_ID}", doc_id=SHEET_ID),
    )

    assert ref is not None
    assert ref.kind == "spreadsheet"
    assert (
        ref.export_url()
        == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
    )


def test_gslides_extension_exports_pptx():
    ref = parse_gdoc_stub(
        "Deck.gslides",
        _stub(url=f"https://docs.google.com/open?id={SHEET_ID}", doc_id=SHEET_ID),
    )

    assert ref is not None
    assert ref.kind == "presentation"
    assert (
        ref.export_url()
        == f"https://docs.google.com/presentation/d/{SHEET_ID}/export?format=pptx"
    )


def test_url_kind_wins_over_gdoc_extension():
    """A real /spreadsheets/d/<id>/ link names the type, so it wins."""
    ref = parse_gdoc_stub(
        "Actually a sheet.gdoc",
        _stub(url=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"),
    )

    assert ref is not None
    assert ref.kind == "spreadsheet"
    assert ref.doc_id == SHEET_ID


def test_url_kind_wins_even_with_doc_id_present():
    ref = parse_gdoc_stub(
        "Actually a sheet.gdoc",
        _stub(
            doc_id=DOC_ID,
            url=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        ),
    )

    assert ref is not None
    # The id still comes from doc_id (preferred), but the URL names the kind.
    assert ref.doc_id == DOC_ID
    assert ref.kind == "spreadsheet"


def test_doc_id_only_resolves():
    ref = parse_gdoc_stub("Pointer.gdoc", _stub(doc_id=DOC_ID))

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)


def test_url_only_resolves():
    ref = parse_gdoc_stub("Pointer.gsheet", _stub(url=OPEN_URL))

    assert ref is not None
    assert ref.doc_id == DOC_ID
    # open?id= does not name the type, so the .gsheet extension decides it.
    assert ref.kind == "spreadsheet"


def test_drive_open_url_resolves():
    ref = parse_gdoc_stub(
        "Pointer.gdoc", _stub(url=f"https://drive.google.com/open?id={DOC_ID}")
    )

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)


def test_domain_scoped_open_url_resolves():
    ref = parse_gdoc_stub(
        "Pointer.gdoc",
        _stub(url=f"https://docs.google.com/a/example.com/open?id={DOC_ID}"),
    )

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)


RESOURCE_KEY_FIELDS = ("resource_key", "resourcekey", "resourceKey")


@pytest.mark.parametrize("field", RESOURCE_KEY_FIELDS)
def test_resource_key_is_captured(field: str):
    ref = parse_gdoc_stub(
        "Pointer.gdoc",
        _stub(url=OPEN_URL, doc_id=DOC_ID, **{field: KEY}),
    )

    assert ref is not None
    assert ref.resource_key == KEY
    assert ref.export_url().endswith(f"&resourcekey={KEY}")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (
            "document",
            f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx",
        ),
        (
            "spreadsheet",
            f"https://docs.google.com/spreadsheets/d/{DOC_ID}/export?format=xlsx",
        ),
        (
            "presentation",
            f"https://docs.google.com/presentation/d/{DOC_ID}/export?format=pptx",
        ),
        (
            "file",
            f"https://drive.google.com/uc?export=download&id={DOC_ID}",
        ),
    ],
)
def test_export_url_appends_resource_key_for_every_kind(kind: str, expected: str):
    ref = GdocRef(kind=kind, doc_id=DOC_ID, resource_key=KEY)

    assert ref.export_url() == f"{expected}&resourcekey={KEY}"


@pytest.mark.parametrize(
    ("filename", "match"),
    [
        ("Form.gform", "Google Forms"),
        ("Drawing.gdraw", "Google Drawings"),
        ("Script.gscript", "Google Apps Script"),
        ("Board.gjam", "Google Jamboard"),
        ("Table.gtable", "Google Table"),
    ],
)
def test_unsupported_shortcut_extensions_name_the_type(filename: str, match: str):
    with pytest.raises(GdocError, match=match):
        parse_gdoc_stub(filename, _stub(doc_id=DOC_ID))
    with pytest.raises(GdocError, match="cannot be converted"):
        parse_gdoc_stub(filename, _stub(doc_id=DOC_ID))


def test_malformed_json_is_rejected():
    with pytest.raises(GdocError, match="does not look like a Google Drive shortcut"):
        parse_gdoc_stub("Broken.gdoc", b"{not json")


def test_non_object_json_is_rejected():
    with pytest.raises(GdocError, match="does not look like a Google Drive shortcut"):
        parse_gdoc_stub("Broken.gsheet", b"[1, 2, 3]")


@pytest.mark.parametrize("payload", [b"{}", b'{"foo": "bar"}', b'{"doc_id": ""}'])
def test_json_without_usable_id_is_rejected(payload: bytes):
    with pytest.raises(GdocError, match=r"Pointer\.gdoc"):
        parse_gdoc_stub("Pointer.gdoc", payload)


def test_non_google_url_in_stub_is_rejected():
    with pytest.raises(GdocError, match=r"Pointer\.gdoc"):
        parse_gdoc_stub("Pointer.gdoc", _stub(url="https://example.com/nope"))


def test_bom_prefixed_json_resolves():
    payload = b"\xef\xbb\xbf" + _stub(url=OPEN_URL, doc_id=DOC_ID)

    ref = parse_gdoc_stub("Pointer.gdoc", payload)

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)


def test_whitespace_padded_json_resolves():
    payload = b"\n  " + _stub(doc_id=DOC_ID) + b"  \n"

    assert parse_gdoc_stub("Pointer.gdoc", payload) == GdocRef(
        kind="document", doc_id=DOC_ID
    )


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("report.pdf", b"%PDF-1.7 not really"),
        ("photo.png", b"\x89PNG\r\n\x1a\n"),
        ("empty.gdoc-not", b""),
        ("no-extension", b"plain text"),
    ],
)
def test_normal_uploads_pass_through(filename: str, content: bytes):
    assert parse_gdoc_stub(filename, content) is None


def test_uppercase_shortcut_extension_is_recognized():
    ref = parse_gdoc_stub("Pointer.GDOC", _stub(url=OPEN_URL, doc_id=DOC_ID))

    assert ref == GdocRef(kind="document", doc_id=DOC_ID)
