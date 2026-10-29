"""``render_pdf`` can write PDF/A-3 (the Factur-X container), and the kill switch defaults on (#1235)."""

from io import BytesIO

import pytest
from pypdf import PdfReader

from common.models import SiteSettings
from common.service.invoice_utils import render_pdf

_HTML = "<html><body><p>Hello</p></body></html>"


def _pdfa_part(pdf: bytes) -> str | None:
    xmp = PdfReader(BytesIO(pdf)).xmp_metadata
    return None if xmp is None else xmp.pdfaid_part


@pytest.fixture
def _plain_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("common.service.invoice_utils.render_to_string", lambda name, context: _HTML)


@pytest.mark.usefixtures("_plain_template")
def test_render_pdf_default_unchanged() -> None:
    """Without the flag the output declares no PDF/A conformance, as before."""
    assert _pdfa_part(render_pdf("ignored.html", {})) is None


@pytest.mark.usefixtures("_plain_template")
def test_render_pdf_a3_declares_part_3() -> None:
    """With ``pdf_a3=True`` the XMP carries ``pdfaid:part`` 3, which ``facturx.embed`` requires."""
    assert _pdfa_part(render_pdf("ignored.html", {}, pdf_a3=True)) == "3"


@pytest.mark.django_db
def test_site_setting_default_true() -> None:
    assert SiteSettings.get_solo().einvoice_facturx_enabled is True
