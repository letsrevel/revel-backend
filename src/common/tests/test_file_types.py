import pytest

from common.file_types import _MIME_EXTENSIONS, extension_for_mime, extension_for_upload
from questionnaires.schema import ALLOWED_QUESTIONNAIRE_MIME_TYPES

ACTIVE_CONTENT_EXTENSIONS = {".html", ".htm", ".svg", ".xhtml", ".js", ".xml"}


@pytest.mark.parametrize("mime_type", sorted(ALLOWED_QUESTIONNAIRE_MIME_TYPES))
def test_every_allowlisted_type_has_an_extension(mime_type: str) -> None:
    """A type added to the allowlist without a mapping must fail here."""
    ext = extension_for_mime(mime_type)
    assert ext.startswith(".")
    assert len(ext) > 1
    assert ext == ext.lower()


def test_no_mapping_yields_active_content_extension() -> None:
    assert not set(_MIME_EXTENSIONS.values()) & ACTIVE_CONTENT_EXTENSIONS


@pytest.mark.parametrize("mime_type", ["text/html", "image/svg+xml", "application/xhtml+xml", "", "foo/bar"])
def test_unknown_type_has_no_extension(mime_type: str) -> None:
    assert extension_for_mime(mime_type) == ""


@pytest.mark.parametrize(
    ("mime_type", "client_name", "expected"),
    [
        ("text/plain", "members.csv", ".csv"),
        ("text/plain", "MEMBERS.CSV", ".csv"),
        ("application/zip", "report.docx", ".docx"),
        ("application/zip", "sheet.xlsx", ".xlsx"),
        ("application/zip", "deck.pptx", ".pptx"),
        ("video/mp4", "voice.m4a", ".m4a"),
    ],
)
def test_generic_type_is_refined_by_allowlisted_suffix(mime_type: str, client_name: str, expected: str) -> None:
    assert extension_for_upload(mime_type, client_name) == expected


@pytest.mark.parametrize(
    ("mime_type", "client_name", "expected"),
    [
        # A suffix outside the allowlist is never used.
        ("text/plain", "page.html", ".txt"),
        ("text/plain", "image.svg", ".txt"),
        ("application/zip", "bundle.jar", ".zip"),
        ("video/mp4", "clip.mov", ".mp4"),
        # An allowlisted suffix on another detected type is not used either.
        ("image/png", "photo.csv", ".png"),
        ("application/pdf", "doc.docx", ".pdf"),
        ("text/plain", "report.docx", ".txt"),
        # No usable client name.
        ("text/plain", None, ".txt"),
        ("text/plain", "", ".txt"),
        ("text/plain", ".csv", ".txt"),
        ("text/html", "page.csv", ""),
    ],
)
def test_other_client_suffixes_are_ignored(mime_type: str, client_name: str | None, expected: str) -> None:
    assert extension_for_upload(mime_type, client_name) == expected
