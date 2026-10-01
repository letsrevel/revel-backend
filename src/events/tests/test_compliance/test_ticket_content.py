"""The EU common-denominator content on ticket PDFs and wallet passes (#1060, #1061, #1063, #1064)."""

import json
import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.template.loader import render_to_string

from events.compliance import get_policy
from events.models import Organization, Ticket, TicketTier
from events.utils import create_ticket_pdf
from wallet.apple.generator import ApplePassGenerator
from wallet.google.builder import build_ticket_payload

pytestmark = pytest.mark.django_db

NOTICE = "This ticket is not a tax invoice or receipt."


@pytest.fixture
def fiscal_org(organization: Organization) -> Organization:
    organization.billing_name = "Org Legal Entity Ltd"
    organization.vat_id = "ATU12345678"
    organization.vat_country_code = "AT"
    organization.save()
    return organization


def _pdf_html(ticket: Ticket) -> str:
    """Render the ticket template with the context ``create_ticket_pdf`` builds."""
    with (
        patch("qrcode.QRCode") as mock_qr,
        patch("weasyprint.HTML") as mock_html,
        patch("events.utils.render_to_string") as mock_render,
    ):
        mock_qr.return_value = Mock()
        mock_html.return_value.write_pdf.return_value = b"fake-pdf"
        mock_render.return_value = "<html></html>"
        create_ticket_pdf(ticket)
    _, kwargs = mock_render.call_args
    return render_to_string("events/ticket.html", t.cast(dict[str, t.Any], kwargs["context"]))


def _apple_back_fields(settings: t.Any, ticket: Ticket) -> dict[str, str]:
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    generator = ApplePassGenerator(signer=MagicMock())
    pass_dict = json.loads(generator._build_pass_json(generator._build_pass_data(ticket)))
    return {field["key"]: field["value"] for field in pass_dict["eventTicket"]["backFields"]}


def _google_modules(ticket: Ticket) -> dict[str, str]:
    obj = build_ticket_payload(ticket)["eventTicketObjects"][0]
    return {module["id"]: module["body"] for module in obj["textModulesData"]}


class TestCommonFields:
    def test_paid_ticket_fields(self, fiscal_org: Organization, ticket: Ticket) -> None:
        fields = {field.key: field.value for field in get_policy(fiscal_org).ticket_fields(ticket)}

        assert fields["organizer"] == "Org Legal Entity Ltd"
        assert fields["tax_id"] == "ATU12345678"
        assert fields["ticket_number"] == "ORG-000001"
        assert fields["issued_at"]
        assert fields["price"] == "EUR 10.00"
        assert fields["notice"] == NOTICE

    def test_free_ticket_says_free_and_org_without_vat_id_has_no_tax_line(
        self, organization: Organization, ticket: Ticket
    ) -> None:
        TicketTier.objects.filter(pk=ticket.tier_id).update(price=Decimal("0"))
        ticket.tier.refresh_from_db()

        fields = {field.key: field.value for field in get_policy(organization).ticket_fields(ticket)}

        assert fields["price"] == "Free"
        assert fields["organizer"] == organization.name
        assert "tax_id" not in fields

    def test_pending_ticket_has_no_number_yet(self, fiscal_org: Organization, ticket: Ticket) -> None:
        pending = Ticket.objects.create(
            event=ticket.event, tier=ticket.tier, user=ticket.user, status=Ticket.TicketStatus.PENDING
        )

        keys = {field.key for field in get_policy(fiscal_org).ticket_fields(pending)}

        assert not {"ticket_number", "issued_at"} & keys


class TestRenderedTickets:
    def test_pdf_carries_the_fiscal_lines(self, fiscal_org: Organization, ticket: Ticket) -> None:
        html = _pdf_html(ticket)

        for text in ("Org Legal Entity Ltd", "ATU12345678", "ORG-000001", "EUR 10.00", NOTICE):
            assert text in html
        # The QR / UUID behaviour is untouched.
        assert str(ticket.id) in html

    def test_apple_pass_back_carries_the_fiscal_lines(
        self, settings: t.Any, fiscal_org: Organization, ticket: Ticket
    ) -> None:
        back = _apple_back_fields(settings, ticket)

        assert back["compliance_ticket_number"] == "ORG-000001"
        assert back["compliance_tax_id"] == "ATU12345678"
        assert back["compliance_price"] == "EUR 10.00"
        assert back["compliance_notice"] == NOTICE
        assert back["ticket_id"] == str(ticket.id)

    def test_google_pass_carries_the_fiscal_lines(self, fiscal_org: Organization, ticket: Ticket) -> None:
        modules = _google_modules(ticket)

        assert modules["ticket_number"] == "ORG-000001"
        assert modules["organizer"] == "Org Legal Entity Ltd"
        assert modules["price"] == "EUR 10.00"
        assert modules["notice"] == NOTICE

    def test_free_ticket_renders_free_on_both_rails(
        self, settings: t.Any, fiscal_org: Organization, ticket: Ticket
    ) -> None:
        TicketTier.objects.filter(pk=ticket.tier_id).update(price=Decimal("0"))
        ticket.tier.refresh_from_db()

        assert _apple_back_fields(settings, ticket)["compliance_price"] == "Free"
        assert _google_modules(ticket)["price"] == "Free"
        assert ">Free<" in _pdf_html(ticket)
