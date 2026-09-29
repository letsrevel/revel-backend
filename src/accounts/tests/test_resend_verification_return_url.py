"""``verify-resend`` ``return_url`` round-trip through the verification email (issue #1024)."""

import re
import typing as t
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts import schema
from accounts.models import RevelUser
from accounts.service import account as account_service
from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

_OAUTH_RETURN_URL = "/oauth/authorize?client_id=x&resource=a&resource=b"


def _sent_link(mock_send: MagicMock) -> str:
    """Extract the single verification link from the plain-text body of the sent email."""
    mock_send.assert_called_once()
    body = mock_send.call_args.kwargs["body"]
    base = re.escape(SiteSettings.get_solo().frontend_base_url)
    links = re.findall(rf"{base}\S+", body)
    assert len(links) == 1
    return str(links[0])


def test_schema_accepts_relative_path_with_query() -> None:
    payload = schema.ResendVerificationSchema(email="u@example.com", return_url=_OAUTH_RETURN_URL)
    assert payload.return_url == _OAUTH_RETURN_URL


def test_schema_return_url_defaults_to_none() -> None:
    assert schema.ResendVerificationSchema(email="u@example.com").return_url is None


def test_schema_shares_register_return_url_constraints() -> None:
    """Both schemas must expose the same pattern and max_length so they can't drift apart."""
    resend = schema.ResendVerificationSchema.model_fields["return_url"]
    register = schema.RegisterUserSchema.model_fields["return_url"]
    assert resend.metadata == register.metadata


@pytest.mark.parametrize(
    "return_url",
    [
        "https://evil",
        "//evil",
        "/\\evil",
        "evil",
        "/path\nX-Injected: 1",
        "/path\r\n",
        "/path\tx",
        "/path with space",
        pytest.param("/" + "a" * 2048, id="2049-chars"),
    ],
)
@patch("accounts.service.account.resend_verification_email")
def test_resend_rejects_non_relative_return_url(mock_resend: MagicMock, client: Client, return_url: str) -> None:
    response = client.post(
        reverse("api:resend-verification-email"),
        data=orjson.dumps({"email": "u@example.com", "return_url": return_url}),
        content_type="application/json",
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "return_url"
    mock_resend.assert_not_called()


@patch("accounts.tasks.email.send_email")
@patch("accounts.tasks.send_account_email.delay", wraps=send_account_email.delay)
def test_resend_link_carries_encoded_return_url(
    mock_delay: MagicMock,
    mock_send: MagicMock,
    unverified_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        account_service.resend_verification_email(unverified_user.email, return_url=_OAUTH_RETURN_URL)

    mock_delay.assert_called_once()
    assert mock_delay.call_args.kwargs["context"] == {"return_url": _OAUTH_RETURN_URL}

    link = _sent_link(mock_send)
    query = parse_qs(urlsplit(link).query, strict_parsing=True)
    assert query["returnUrl"] == [_OAUTH_RETURN_URL]
    assert "%2Foauth%2Fauthorize%3Fclient_id%3Dx%26resource%3Da%26resource%3Db" in link


@patch("accounts.tasks.email.send_email")
@patch("accounts.tasks.send_account_email.delay", wraps=send_account_email.delay)
def test_resend_link_unchanged_without_return_url(
    mock_delay: MagicMock,
    mock_send: MagicMock,
    unverified_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        account_service.resend_verification_email(unverified_user.email)

    # The dispatched message keeps its exact pre-return_url shape (no ``context`` kwarg).
    token = mock_delay.call_args.kwargs["token"]
    mock_delay.assert_called_once_with(AccountEmail.VERIFICATION, unverified_user.email, token=token)
    assert "returnUrl" not in _sent_link(mock_send)


# transaction=True: the resend dispatches via transaction.on_commit; in default pytest-django
# mode the wrapping transaction is rolled back and the callback never fires.
@pytest.mark.django_db(transaction=True)
@patch("accounts.tasks.send_account_email.delay")
def test_endpoint_threads_return_url_to_task(mock_delay: MagicMock, client: Client, unverified_user: RevelUser) -> None:
    response = client.post(
        reverse("api:resend-verification-email"),
        data=orjson.dumps({"email": unverified_user.email, "return_url": _OAUTH_RETURN_URL}),
        content_type="application/json",
    )

    assert response.status_code == 200
    mock_delay.assert_called_once()
    assert mock_delay.call_args.kwargs["context"] == {"return_url": _OAUTH_RETURN_URL}
