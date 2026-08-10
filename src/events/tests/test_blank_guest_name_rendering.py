"""Blank guest_name renders clean PDF context and Wallet pass (#845)."""

import json
import typing as t
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.template.loader import render_to_string

from events.models import Ticket
from events.utils import create_ticket_pdf
from wallet.apple.generator import ApplePassGenerator

pytestmark = pytest.mark.django_db


def _ticket_pdf_context(ticket: Ticket) -> dict[str, t.Any]:
    """Capture the context ``create_ticket_pdf`` builds for the ticket template."""
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
    return t.cast(dict[str, t.Any], kwargs["context"])


def test_pdf_template_omits_guest_row_for_blank_name(ticket: Ticket) -> None:
    """A blank guest_name drops the guest row but keeps the purchaser row."""
    ticket.guest_name = ""
    ticket.save(update_fields=["guest_name"])

    context = _ticket_pdf_context(ticket)
    assert context["guest_name"] == ""

    html = render_to_string("events/ticket.html", context)

    assert ">Guest<" not in html
    assert ">Purchased by<" in html


def test_pdf_template_keeps_guest_row_for_named_ticket(ticket: Ticket) -> None:
    """A named ticket still renders the guest row (control for the blank-name guard)."""
    ticket.guest_name = "Zaphod Beeblebrox"
    ticket.save(update_fields=["guest_name"])

    html = render_to_string("events/ticket.html", _ticket_pdf_context(ticket))

    assert ">Guest<" in html
    assert "Zaphod Beeblebrox" in html


def test_wallet_pass_omits_guest_field_for_blank_name(settings: t.Any, ticket: Ticket) -> None:
    """A blank guest_name produces a pass with no GUEST field."""
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    ticket.guest_name = ""
    ticket.save(update_fields=["guest_name"])

    generator = ApplePassGenerator(signer=MagicMock())
    pass_dict = json.loads(generator._build_pass_json(generator._build_pass_data(ticket)))

    event_ticket = pass_dict["eventTicket"]
    assert all(field["key"] != "guest" for field in event_ticket["auxiliaryFields"])
    assert all(field["key"] != "guest_name" for field in event_ticket["backFields"])
