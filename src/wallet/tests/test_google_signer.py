"""Tests for the Google Wallet fat-JWT signer."""

import typing as t

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from wallet.google.signer import SAVE_URL_BASE, GooglePassSigner, GooglePassSignerError

pytestmark = pytest.mark.django_db


def test_save_url_signs_decodable_jwt(
    google_wallet_configured_settings: None, mock_private_key: rsa.RSAPrivateKey
) -> None:
    payload = {"eventTicketClasses": [{"id": "x"}], "eventTicketObjects": [{"id": "y"}]}
    url = GooglePassSigner().save_url(payload)

    assert url.startswith(SAVE_URL_BASE)
    token = url.removeprefix(SAVE_URL_BASE)
    claims = pyjwt.decode(token, mock_private_key.public_key(), algorithms=["RS256"], audience="google")

    assert claims["iss"] == "wallet@test-project.iam.gserviceaccount.com"
    assert claims["aud"] == "google"
    assert claims["typ"] == "savetowallet"
    assert claims["payload"] == payload
    assert "iat" in claims
    assert "exp" not in claims  # emailed links must never go stale
    assert isinstance(claims["origins"], list) and claims["origins"]


def test_signer_raises_when_unconfigured(google_wallet_not_configured: None) -> None:
    with pytest.raises(GooglePassSignerError):
        GooglePassSigner()


def test_signer_raises_on_missing_key_file(settings: t.Any) -> None:
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = "/nonexistent/sa.json"
    with pytest.raises(GooglePassSignerError):
        GooglePassSigner()


def test_ticket_save_url_facade(google_wallet_configured_settings: None, ticket: t.Any) -> None:
    from wallet.google.service import ticket_save_url

    assert ticket_save_url(ticket).startswith(SAVE_URL_BASE)
