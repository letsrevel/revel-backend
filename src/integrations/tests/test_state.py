"""Signed connect state: round-trips, expires, rejects tampering and wrong token types."""

import uuid
from datetime import timedelta

import pytest
from freezegun import freeze_time

from integrations.exceptions import IntegrationError
from integrations.schema import IntegrationErrorCode
from integrations.service import state as state_service


def test_round_trip() -> None:
    org, user = uuid.uuid4(), uuid.uuid4()
    token = state_service.mint_state(organization_id=org, user_id=user, provider="fake")
    payload = state_service.validate_state(token)
    assert (payload.organization_id, payload.user_id, payload.provider) == (org, user, "fake")
    assert payload.jti


def test_expired_state_rejected() -> None:
    with freeze_time("2026-09-04 10:00:00"):
        token = state_service.mint_state(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), provider="fake")
    with freeze_time("2026-09-04 10:00:00") as frozen:
        frozen.tick(timedelta(seconds=601))
        with pytest.raises(IntegrationError) as exc:
            state_service.validate_state(token)
    assert exc.value.code == IntegrationErrorCode.STATE_INVALID


def test_tampered_state_rejected() -> None:
    token = state_service.mint_state(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), provider="fake")
    with pytest.raises(IntegrationError):
        state_service.validate_state(token[:-3] + "abc")


def test_oidc_login_token_is_not_a_connect_state() -> None:
    from accounts.jwt import create_oidc_login_token

    other = create_oidc_login_token(user_id=str(uuid.uuid4()), return_url="/", jti="x")
    with pytest.raises(IntegrationError):
        state_service.validate_state(other)


def test_cookie_match_is_constant_time_and_ascii_safe() -> None:
    assert state_service.state_matches_cookie("abc", "abc") is True
    assert state_service.state_matches_cookie("abc", "abd") is False
    assert state_service.state_matches_cookie(None, "abc") is False
    assert state_service.state_matches_cookie("abc", "abç") is False
