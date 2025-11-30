"""Tests for wallet/controllers.py."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

import wallet.controllers
from events.models import Ticket
from wallet.controllers import get_apple_pass_generator

pytestmark = pytest.mark.django_db


class TestGetApplePassGenerator:
    """Tests for get_apple_pass_generator function."""

    def test_returns_generator(self) -> None:
        """Should return an ApplePassGenerator instance."""
        wallet.controllers._apple_pass_generator = None

        with patch("wallet.controllers.ApplePassGenerator") as MockGenerator:
            mock_instance = MagicMock()
            MockGenerator.return_value = mock_instance

            result = get_apple_pass_generator()

            assert result is mock_instance
            MockGenerator.assert_called_once()

        wallet.controllers._apple_pass_generator = None

    def test_caches_generator(self) -> None:
        """Should cache and reuse the generator."""
        wallet.controllers._apple_pass_generator = None

        with patch("wallet.controllers.ApplePassGenerator") as MockGenerator:
            mock_instance = MagicMock()
            MockGenerator.return_value = mock_instance

            result1 = get_apple_pass_generator()
            result2 = get_apple_pass_generator()

            assert result1 is result2
            MockGenerator.assert_called_once()

        wallet.controllers._apple_pass_generator = None


# --- Controller Integration Tests ---


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
