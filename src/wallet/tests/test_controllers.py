"""Tests for wallet/controllers.py."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Ticket

pytestmark = pytest.mark.django_db


class TestTicketWalletControllerDownloadApplePass:
    """Tests for TicketWalletController.download_apple_pass endpoint."""

    def test_returns_503_when_not_configured(
        self,
        apple_wallet_not_configured: None,
        member_client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 503 when Apple Wallet is not configured."""
        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 503

    def test_returns_404_for_nonexistent_ticket(
        self,
        apple_wallet_configured: None,
        member_client: Client,
    ) -> None:
        """Should return 404 for nonexistent ticket."""
        fake_id = uuid4()
        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": fake_id})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_returns_404_for_other_users_ticket(
        self,
        apple_wallet_configured: None,
        nonmember_client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 404 when trying to access another user's ticket."""
        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = nonmember_client.get(url)

        assert response.status_code == 404

    def test_returns_401_for_unauthenticated_user(
        self,
        client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 401 for unauthenticated requests."""
        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = client.get(url)

        assert response.status_code == 401

    def test_returns_pkpass_file_on_success(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        ticket: Ticket,
        mock_pass_generator: MagicMock,
    ) -> None:
        """Should return .pkpass file with correct content type."""
        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/vnd.apple.pkpass"
        assert "Content-Disposition" in response
        assert ".pkpass" in response["Content-Disposition"]

    def test_excludes_cancelled_tickets(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        ticket: Ticket,
    ) -> None:
        """Should not return passes for cancelled tickets."""
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save()

        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_allows_pending_tickets(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        ticket: Ticket,
        mock_pass_generator: MagicMock,
    ) -> None:
        """Should allow downloading passes for pending tickets."""
        ticket.status = Ticket.TicketStatus.PENDING
        ticket.save()

        url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 200


class TestTicketWalletControllerDownloadPdf:
    """Tests for TicketWalletController.download_pdf endpoint."""

    def test_returns_pdf_with_correct_content_type(
        self,
        member_client: Client,
        ticket: Ticket,
        mock_pdf_generator: MagicMock,
    ) -> None:
        """Should return PDF with application/pdf content type and attachment disposition."""
        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert "Content-Disposition" in response
        assert ".pdf" in response["Content-Disposition"]
        assert response.content == b"%PDF-mock-content"

    def test_returns_404_for_nonexistent_ticket(
        self,
        member_client: Client,
    ) -> None:
        """Should return 404 for a ticket that does not exist."""
        fake_id = uuid4()
        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": fake_id})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_returns_404_for_other_users_ticket(
        self,
        nonmember_client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 404 when a user tries to access another user's ticket."""
        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = nonmember_client.get(url)

        assert response.status_code == 404

    def test_returns_401_for_unauthenticated_user(
        self,
        client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 401 for unauthenticated requests."""
        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = client.get(url)

        assert response.status_code == 401

    def test_excludes_cancelled_tickets(
        self,
        member_client: Client,
        ticket: Ticket,
    ) -> None:
        """Should return 404 for cancelled tickets."""
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save()

        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_allows_pending_tickets(
        self,
        member_client: Client,
        ticket: Ticket,
        mock_pdf_generator: MagicMock,
    ) -> None:
        """Should allow downloading PDF for pending tickets."""
        ticket.status = Ticket.TicketStatus.PENDING
        ticket.save()

        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"

    def test_content_disposition_contains_ticket_id_prefix(
        self,
        member_client: Client,
        ticket: Ticket,
        mock_pdf_generator: MagicMock,
    ) -> None:
        """Should include ticket ID prefix in the filename."""
        url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
        response = member_client.get(url)

        ticket_id_short = str(ticket.id).split("-")[0]
        assert ticket_id_short in response["Content-Disposition"]

    def test_redirects_to_signed_url_when_file_cached(
        self,
        member_client: Client,
        ticket: Ticket,
        mock_pdf_generator: MagicMock,
    ) -> None:
        """Should redirect via the fast path when cache is valid."""
        signed = "/media/protected/tickets/pdf/ticket_abc.pdf?exp=123&sig=abc"
        with (
            patch("wallet.controllers.ticket_file_service.is_cache_valid", return_value=True),
            patch("wallet.controllers.get_file_url", return_value=signed),
            patch("wallet.controllers.ticket_file_service.get_or_generate_pdf") as mock_generate,
        ):
            url = reverse("api:ticket_pdf_download", kwargs={"ticket_id": ticket.id})
            response = member_client.get(url)

        assert response.status_code == 302
        assert response["Location"] == signed
        mock_generate.assert_not_called()
