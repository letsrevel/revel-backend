"""Attendee invoices and credit notes state that Revel issues them on the organizer's behalf (#1258).

Art. 220(1) of Directive 2006/112/EC ("in his name and on his behalf, by a third party"); AT UStR
Rz 1505, 1530-1532; DE § 14 Abs. 2 S. 7 UStG. The issuer is the platform operator's legal name from
``SiteSettings.platform_business_name`` (the brand name when none is configured), snapshotted on the
document when it is created so a later rename never rewrites an issued document.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from common.models import SiteSettings
from common.service.invoice_utils import issuance_statement, platform_issuer_name
from events.models import Event, Organization, TicketTier
from events.service import attendee_invoice_service
from events.service.attendee_invoice_draft_service import issue_draft_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Revel Platform e.U."
ISSUED_BY = f"Issued by {LEGAL_NAME} in the name and on behalf of"


def _set_platform_name(name: str) -> None:
    settings = SiteSettings.get_solo()
    settings.platform_business_name = name
    settings.save()


@pytest.fixture
def platform_name() -> None:
    _set_platform_name(LEGAL_NAME)


def test_issuer_is_the_platform_legal_name_or_the_brand() -> None:
    _set_platform_name("")
    assert platform_issuer_name() == "Revel"
    assert issuance_statement("", "ACME SRL") == "Issued by Revel in the name and on behalf of ACME SRL."
    _set_platform_name(LEGAL_NAME)
    assert platform_issuer_name() == LEGAL_NAME
    assert (
        issuance_statement("Old Name GmbH", "ACME SRL")
        == "Issued by Old Name GmbH in the name and on behalf of ACME SRL."
    )


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_generated_invoice_snapshots_the_issuer(
    _mail: t.Any,
    _pdf: t.Any,
    platform_name: None,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    _create_payment(
        user=member_user,
        event=event,
        tier=event_ticket_tier,
        session_id="cs_issuer",
        buyer_billing_snapshot=_default_billing_snapshot(),
    )
    invoice = attendee_invoice_service.generate_attendee_invoice("cs_issuer")
    assert invoice is not None
    assert invoice.issuer_name == LEGAL_NAME


def test_invoice_pdf_renders_the_snapshot_not_the_current_name(
    platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _create_draft(organization, event, member_user)  # seller_name "ACME SRL"
    invoice.issuer_name = "Old Name GmbH"
    invoice.save()
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf") as render:
        attendee_invoice_service.ensure_pdf_exists(invoice)
    template, context = render.call_args.args
    assert context["issuance_statement"] == "Issued by Old Name GmbH in the name and on behalf of ACME SRL."
    html = render_to_string(template, context)
    assert "Issued by Old Name GmbH in the name and on behalf of ACME SRL." in html
    assert "Issuer" in html


def test_legacy_invoice_without_a_snapshot_names_the_current_operator(
    platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _create_draft(organization, event, member_user)
    assert invoice.issuer_name == ""
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf") as render:
        attendee_invoice_service.ensure_pdf_exists(invoice)
    assert render.call_args.args[1]["issuance_statement"] == f"{ISSUED_BY} ACME SRL."


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
def test_issuing_a_legacy_draft_stamps_the_snapshot(
    _pdf: t.Any, platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    _make_org_invoicing_ready(organization)
    invoice = _create_draft(organization, event, member_user)
    invoice.total_vat = Decimal("0.00")
    invoice.total_net = Decimal("100.00")
    invoice.line_items = [{**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": "100.00"}]
    invoice.save()
    issued = issue_draft_invoice(invoice)
    issued.refresh_from_db()
    assert issued.issuer_name == LEGAL_NAME


def test_credit_note_snapshots_and_renders_its_own_issuer(
    platform_name: None, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], seller_name="Kleine Bühne")
    note = make_credit_note(invoice, [line("24.90", "13.00")])
    note.issuer_name = "Old Name GmbH"
    note.save()
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf") as render:
        attendee_invoice_service.ensure_credit_note_pdf_exists(note)
    template, context = render.call_args.args
    assert context["issuance_statement"] == "Issued by Old Name GmbH in the name and on behalf of Kleine Bühne."
    assert "Issued by Old Name GmbH in the name and on behalf of Kleine Bühne." in render_to_string(template, context)


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_generated_credit_note_snapshots_the_issuer(
    _mail: t.Any,
    _pdf: t.Any,
    platform_name: None,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    payment = _create_payment(
        user=member_user,
        event=event,
        tier=event_ticket_tier,
        session_id="cs_issuer_cn",
        buyer_billing_snapshot=_default_billing_snapshot(),
    )
    assert attendee_invoice_service.generate_attendee_invoice("cs_issuer_cn") is not None
    _set_platform_name("Renamed Platform GmbH")
    note = attendee_invoice_service.generate_attendee_credit_note("cs_issuer_cn", [payment.id])
    assert note is not None
    assert note.issuer_name == "Renamed Platform GmbH"  # its own snapshot, taken when the note is created
