import pytest

from common.file_types import _MIME_EXTENSIONS, extension_for_mime
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
