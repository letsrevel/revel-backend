"""Tests for Google Wallet availability gating."""

import typing as t

import pytest

from events.utils import google_wallet_configured


@pytest.fixture
def google_wallet_settings(settings: t.Any) -> None:
    """Configure Google Wallet settings for tests."""
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = "/path/sa.json"


@pytest.fixture
def google_wallet_unset(settings: t.Any) -> None:
    """Clear Google Wallet settings for tests."""
    settings.GOOGLE_WALLET_ISSUER_ID = ""
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = ""


def test_configured_when_both_settings_set(google_wallet_settings: None) -> None:
    assert google_wallet_configured() is True


def test_not_configured_when_unset(google_wallet_unset: None) -> None:
    assert google_wallet_configured() is False


def test_not_configured_when_key_path_missing(settings: t.Any) -> None:
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = ""
    assert google_wallet_configured() is False


@pytest.mark.django_db
def test_ticket_google_pass_available(google_wallet_settings: None, ticket: t.Any) -> None:
    assert ticket.google_pass_available is True


@pytest.mark.django_db
def test_ticket_google_pass_unavailable(google_wallet_unset: None, ticket: t.Any) -> None:
    assert ticket.google_pass_available is False
