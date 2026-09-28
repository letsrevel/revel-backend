"""Registration ``return_url`` round-trip through the verification email (issue #1022)."""

import re
import typing as t
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError

from accounts import schema
from accounts.models import RevelUser
from accounts.service import account as account_service
from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

_OAUTH_RETURN_URL = "/oauth/authorize?client_id=x&resource=a&resource=b"


def _payload(**overrides: t.Any) -> dict[str, t.Any]:
    return {
        "email": "newuser@example.com",
        "password1": "a-Strong-password-123!",
        "password2": "a-Strong-password-123!",
        "first_name": "New",
        "last_name": "User",
        "accept_toc_and_privacy": True,
        **overrides,
    }


def _sent_link(mock_send: MagicMock) -> str:
    """Extract the single verification link from the plain-text body of the sent email."""
    mock_send.assert_called_once()
    body = mock_send.call_args.kwargs["body"]
    base = re.escape(SiteSettings.get_solo().frontend_base_url)
    links = re.findall(rf"{base}\S+", body)
    assert len(links) == 1
    assert links[0] in mock_send.call_args.kwargs["html_body"].replace("&amp;", "&")
    return str(links[0])


def test_schema_accepts_relative_path_with_query() -> None:
    payload = schema.RegisterUserSchema(**_payload(return_url=_OAUTH_RETURN_URL))
    assert payload.return_url == _OAUTH_RETURN_URL


def test_schema_return_url_defaults_to_none() -> None:
    assert schema.RegisterUserSchema(**_payload()).return_url is None


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
        pytest.param("/path\x9f", id="c1-control"),
        pytest.param("/\x85evil", id="c1-next-line"),
        pytest.param("/" + "a" * 2048, id="2049-chars"),
    ],
)
@patch("accounts.service.account.register_user")
def test_register_rejects_non_relative_return_url(mock_register: MagicMock, client: Client, return_url: str) -> None:
    response = client.post(
        reverse("api:register-account"), data=_payload(return_url=return_url), content_type="application/json"
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "return_url"
    mock_register.assert_not_called()


@patch("accounts.tasks.email.send_email")
@patch("accounts.tasks.send_account_email.delay", wraps=send_account_email.delay)
def test_verification_link_carries_encoded_return_url(
    mock_delay: MagicMock, mock_send: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    payload = schema.RegisterUserSchema(**_payload(return_url=_OAUTH_RETURN_URL))

    with django_capture_on_commit_callbacks(execute=True):
        user, token = account_service.register_user(payload)

    # Carried in the pre-existing ``context`` kwarg so not-yet-upgraded workers accept the message.
    mock_delay.assert_called_once_with(
        AccountEmail.VERIFICATION, user.email, token=token, context={"return_url": _OAUTH_RETURN_URL}
    )

    link = _sent_link(mock_send)
    base = SiteSettings.get_solo().frontend_base_url
    assert link == (
        f"{base}/login/confirm-email?token={token}"
        "&returnUrl=%2Foauth%2Fauthorize%3Fclient_id%3Dx%26resource%3Da%26resource%3Db"
    )
    query = parse_qs(urlsplit(link).query, strict_parsing=True)
    assert query == {"token": [token], "returnUrl": [_OAUTH_RETURN_URL]}


@patch("accounts.tasks.email.send_email")
@patch("accounts.tasks.send_account_email.delay", wraps=send_account_email.delay)
def test_verification_link_unchanged_without_return_url(
    mock_delay: MagicMock, mock_send: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        user, token = account_service.register_user(schema.RegisterUserSchema(**_payload()))

    link = _sent_link(mock_send)
    assert "returnUrl" not in link
    assert link == f"{SiteSettings.get_solo().frontend_base_url}/login/confirm-email?token={token}"
    # The dispatched message keeps its exact pre-return_url shape.
    mock_delay.assert_called_once_with(AccountEmail.VERIFICATION, user.email, token=token)


@patch("accounts.tasks.email.send_email")
def test_duplicate_registration_ignores_return_url(mock_send: MagicMock, unverified_user: RevelUser) -> None:
    """The anti-enumeration resend for an existing unverified user never carries returnUrl."""
    payload = schema.RegisterUserSchema(**_payload(email=unverified_user.email, return_url=_OAUTH_RETURN_URL))

    with pytest.raises(HttpError):
        account_service.register_user(payload)

    link = _sent_link(mock_send)
    assert "returnUrl" not in link
    assert "/login/confirm-email?token=" in link


@pytest.mark.parametrize("context", [None, {}, {"unrelated": "x"}], ids=["no-context", "empty", "no-return-url"])
@patch("accounts.tasks.email.send_email")
def test_task_without_return_url_in_context_builds_old_link(
    mock_send: MagicMock, context: dict[str, str] | None
) -> None:
    send_account_email(AccountEmail.VERIFICATION, "u@example.com", token="tok", context=context)

    assert _sent_link(mock_send) == f"{SiteSettings.get_solo().frontend_base_url}/login/confirm-email?token=tok"


@patch("accounts.tasks.email.send_email")
def test_task_ignores_return_url_on_other_link_emails(mock_send: MagicMock) -> None:
    """Only the verification link carries ``returnUrl``, whatever a caller puts in ``context``."""
    send_account_email(AccountEmail.PASSWORD_RESET, "u@example.com", token="tok", context={"return_url": "/x"})

    assert "returnUrl" not in _sent_link(mock_send)
