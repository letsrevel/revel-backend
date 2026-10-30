"""Attendee invoices and credit notes state that Revel issues them on the organizer's behalf (#1258).

Art. 220(1) of Directive 2006/112/EC ("in his name and on his behalf, by a third party"); AT UStR
Rz 1505, 1530-1532; DE § 14 Abs. 2 S. 7 UStG. The issuer is the platform operator's legal name from
``SiteSettings.platform_business_name``, the brand name when none is configured.
"""

from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from events.service import attendee_invoice_service
from events.tests.test_attendee_invoice._helpers import MOCK_RENDER_PDF, _create_draft
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

ISSUED_BY = "Issued by Revel Platform e.U. in the name and on behalf of"


@pytest.fixture
def platform_name() -> None:
    settings = SiteSettings.get_solo()
    settings.platform_business_name = "Revel Platform e.U."
    settings.save()


def test_issuer_is_the_platform_legal_name_or_the_brand() -> None:
    settings = SiteSettings.get_solo()
    settings.platform_business_name = ""
    settings.save()
    assert attendee_invoice_service._issuer_name() == "Revel"
    settings.platform_business_name = "Revel Platform e.U."
    settings.save()
    assert attendee_invoice_service._issuer_name() == "Revel Platform e.U."


def test_invoice_pdf_states_third_party_issuance(
    platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _create_draft(organization, event, member_user)  # seller_name "ACME SRL"
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf") as render:
        attendee_invoice_service.ensure_pdf_exists(invoice)
    template, context = render.call_args.args
    assert context["issuer_name"] == "Revel Platform e.U."
    html = render_to_string(template, context)
    assert f"{ISSUED_BY} ACME SRL." in html
    assert "Issuer" in html


def test_credit_note_pdf_states_third_party_issuance(
    platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], seller_name="Kleine Bühne")
    note = make_credit_note(invoice, [line("24.90", "13.00")])
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf") as render:
        attendee_invoice_service.ensure_credit_note_pdf_exists(note)
    template, context = render.call_args.args
    assert context["issuer_name"] == "Revel Platform e.U."
    html = render_to_string(template, context)
    assert f"{ISSUED_BY} Kleine Bühne." in html


def test_templates_fall_back_to_the_brand_without_an_issuer(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    """Rendered outside the service (branding guards, previews), the line still names the platform."""
    invoice = _create_draft(organization, event, member_user)
    html = render_to_string(
        "invoices/attendee_invoice.html",
        {"invoice": invoice, "is_draft": True, "is_export": False, "is_virtual_interim": False},
    )
    assert "Issued by Revel in the name and on behalf of ACME SRL." in html
