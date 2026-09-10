"""Wallet signer/generator failures answer 503, not an opaque 500.

Covers both the handler table itself (status, body, log) and the wiring from
:meth:`wallet.apps.WalletConfig.ready` — a handler nobody registers is dead code.
"""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test import RequestFactory
from django.test.client import Client
from django.urls import reverse
from structlog.testing import capture_logs

from events.models import Ticket
from wallet.apple.generator import ApplePassGeneratorError
from wallet.apple.signer import ApplePassSignerError
from wallet.exception_handlers import HANDLERS, PASS_UNAVAILABLE_MESSAGE
from wallet.google.signer import GooglePassSignerError
from wallet.schema import WalletPassErrorCode

WALLET_ERRORS: list[type[Exception]] = [ApplePassSignerError, ApplePassGeneratorError, GooglePassSignerError]


@pytest.mark.parametrize("exc_type", WALLET_ERRORS)
def test_handler_renders_static_503_with_a_stable_code(exc_type: type[Exception]) -> None:
    """Each wallet error renders the fixed message plus the machine-readable code.

    The un-``code``d 503 on the same routes means "Wallet is not configured";
    the frontend tells the two apart on ``code``, never on ``detail``.
    """
    response = HANDLERS[exc_type](RequestFactory().get("/"), exc_type("boom"))

    assert response.status_code == 503
    body = orjson.loads(response.content)
    assert body == {"detail": str(PASS_UNAVAILABLE_MESSAGE), "code": "wallet_pass_unavailable"}
    assert body["code"] == WalletPassErrorCode.PASS_UNAVAILABLE.value


@pytest.mark.parametrize("exc_type", WALLET_ERRORS)
def test_handler_does_not_leak_the_exception_message(exc_type: type[Exception]) -> None:
    """``str(exc)`` carries filesystem paths — the body must be static instead."""
    exc = exc_type("Certificate not found: /app/certs/pass.pem")

    response = HANDLERS[exc_type](RequestFactory().get("/"), exc)

    assert "/app/certs" not in response.content.decode()


@pytest.mark.parametrize("exc_type", WALLET_ERRORS)
def test_handler_logs_the_failure_with_traceback(exc_type: type[Exception]) -> None:
    """The 503 must stay diagnosable in Loki (the cert-permissions incident)."""
    exc = exc_type("Certificate not found: /app/certs/pass.pem")

    with capture_logs() as logs:
        HANDLERS[exc_type](RequestFactory().get("/wallet/apple"), exc)

    assert len(logs) == 1
    entry = logs[0]
    assert entry["event"] == "wallet_pass_unavailable"
    assert entry["log_level"] == "error"
    assert entry["exception_type"] == exc_type.__name__
    assert entry["path"] == "/wallet/apple"
    assert entry["exc_info"] is exc


def test_every_wallet_error_is_registered_on_the_api() -> None:
    """WalletConfig.ready must have installed the table on the global API."""
    from api.api import api

    for exc_type in WALLET_ERRORS:
        assert api._exception_handlers[exc_type] is HANDLERS[exc_type]


@pytest.mark.django_db
def test_apple_pass_generation_failure_returns_503(
    apple_wallet_configured: None,
    member_client: Client,
    ticket: Ticket,
) -> None:
    """A signing/generation failure is a 503, not a 500."""
    url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})

    with patch(
        "events.service.ticket_file_service.get_or_generate_pkpass",
        side_effect=ApplePassGeneratorError("Failed to generate pass: [Errno 13] /app/certs/pass.pem"),
    ):
        response = member_client.get(url)

    assert response.status_code == 503
    assert response.json() == {"detail": str(PASS_UNAVAILABLE_MESSAGE), "code": "wallet_pass_unavailable"}


@pytest.mark.django_db
def test_google_save_link_signer_failure_returns_503(
    google_wallet_configured_settings: None,
    member_client: Client,
    ticket: Ticket,
) -> None:
    """An unreadable service-account key is a 503, not a 500."""
    url = reverse("api:ticket_google_wallet_pass", kwargs={"ticket_id": ticket.id})

    with patch(
        "wallet.google.service.ticket_save_url",
        side_effect=GooglePassSignerError("Cannot load Google Wallet service account key: [Errno 13]"),
    ):
        response = member_client.get(url, {"format": "json"})

    assert response.status_code == 503
    assert response.json() == {"detail": str(PASS_UNAVAILABLE_MESSAGE), "code": "wallet_pass_unavailable"}


@pytest.mark.django_db
def test_membership_pass_generation_failure_returns_503(
    apple_wallet_configured: None,
    member_client: Client,
    member: t.Any,
) -> None:
    """The membership rail shares the mapping (same generator, same errors)."""
    url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})

    with patch(
        "wallet.controllers.get_apple_pass_generator",
        side_effect=ApplePassSignerError("Certificate not found: /app/certs/pass.pem"),
    ):
        response = member_client.get(url)

    assert response.status_code == 503
    assert response.json() == {"detail": str(PASS_UNAVAILABLE_MESSAGE), "code": "wallet_pass_unavailable"}


@pytest.mark.django_db
def test_not_configured_503_carries_no_code(
    apple_wallet_not_configured: None,
    member_client: Client,
    ticket: Ticket,
) -> None:
    """The other 503 on the same route stays un-``code``d.

    The frontend renders "Wallet is not configured" for that one and a
    try-again message for the coded one, so the two bodies must stay
    distinguishable without reading the translated ``detail``.
    """
    url = reverse("api:ticket_apple_wallet_pass", kwargs={"ticket_id": ticket.id})

    response = member_client.get(url)

    assert response.status_code == 503
    assert "code" not in response.json()
