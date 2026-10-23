"""Account emails carry a tenant ``return_host`` hint but their token links stay on the platform (#1156).

SECURITY regression guard: a custom domain's DNS belongs to the organization owner, so an
account-security token must never be placed on a tenant-domain link, even when the request's
``Origin`` is a perfectly valid tenant origin.
"""

import re
import typing as t
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings
from events.models import Organization, OrganizationDomain
from events.service import custom_domain_service, tenant_service

# transaction=True: most account services dispatch the email via transaction.on_commit, which never
# fires inside pytest-django's rolled-back wrapping transaction.
pytestmark = pytest.mark.django_db(transaction=True)

HOST = "events.example.com"
TENANT_ORIGIN = f"https://{HOST}"
PASSWORD = "strong-password-123!"  # noqa: S105 - test fixture password


@pytest.fixture(autouse=True)
def tenant_domain(django_user_model: type[RevelUser], settings: t.Any) -> None:
    """An organization with an ACTIVE custom domain, and a pinned platform base URL."""
    settings.DEBUG = False
    owner = django_user_model.objects.create_user(username="tenant_owner", email="tenant_owner@example.com")
    org = Organization.objects.create(name="Tenant Org", slug="tenant-org", owner=owner, custom_domain_enabled=True)
    domain = OrganizationDomain.objects.create(organization=org, hostname=HOST)
    custom_domain_service.activate_domain(domain)


@pytest.fixture(autouse=True)
def run_email_task_inline() -> t.Generator[MagicMock]:
    """Run ``send_account_email`` inline and capture the outgoing email."""
    with (
        patch("accounts.tasks.send_account_email.delay", side_effect=send_account_email),
        patch("accounts.tasks.email.send_email") as mock_send,
    ):
        yield mock_send


def _action_link(mock_send: MagicMock) -> str:
    """The single account link in the plain-text body of the sent email."""
    mock_send.assert_called()
    body = mock_send.call_args_list[0].kwargs["body"]
    base = re.escape(SiteSettings.get_solo().frontend_base_url)
    links = re.findall(rf"{base}/\S+", body)
    assert len(links) == 1, body
    return str(links[0])


def _assert_platform_link_with_hint(link: str, path: str) -> None:
    """The link is on the platform, carries the token, and has the hint as a trailing query param."""
    platform = SiteSettings.get_solo().frontend_base_url
    assert link.startswith(f"{platform}{path}?token=")
    assert link.endswith(f"&return_host={HOST}")
    # The tenant host appears only as the hint value, never as the link's host.
    assert link.count(HOST) == 1


def test_register_link_is_platform_with_hint(client: Client, run_email_task_inline: MagicMock) -> None:
    payload = {
        "email": "newuser@example.com",
        "password1": "a-Strong-password-123!",
        "password2": "a-Strong-password-123!",
        "first_name": "New",
        "last_name": "User",
        "accept_toc_and_privacy": True,
    }
    response = client.post(
        reverse("api:register-account"),
        data=orjson.dumps(payload),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    assert response.status_code == 201
    _assert_platform_link_with_hint(_action_link(run_email_task_inline), "/login/confirm-email")


def test_register_with_return_url_keeps_it_before_the_hint(client: Client, run_email_task_inline: MagicMock) -> None:
    payload = {
        "email": "newuser@example.com",
        "password1": "a-Strong-password-123!",
        "password2": "a-Strong-password-123!",
        "first_name": "New",
        "last_name": "User",
        "accept_toc_and_privacy": True,
        "return_url": "/oauth/authorize?client_id=x",
    }
    client.post(
        reverse("api:register-account"),
        data=orjson.dumps(payload),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    link = _action_link(run_email_task_inline)
    assert "&returnUrl=%2Foauth%2Fauthorize%3Fclient_id%3Dx&return_host=" + HOST in link


def test_resend_verification_link_is_platform_with_hint(
    client: Client, unverified_user: RevelUser, run_email_task_inline: MagicMock
) -> None:
    response = client.post(
        reverse("api:resend-verification-email"),
        data=orjson.dumps({"email": unverified_user.email}),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    assert response.status_code == 200
    _assert_platform_link_with_hint(_action_link(run_email_task_inline), "/login/confirm-email")


def test_password_reset_link_is_platform_with_hint(
    client: Client, user: RevelUser, run_email_task_inline: MagicMock
) -> None:
    response = client.post(
        reverse("api:reset-password-request"),
        data=orjson.dumps({"email": user.email}),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    assert response.status_code == 200
    _assert_platform_link_with_hint(_action_link(run_email_task_inline), "/login/reset-password")


def test_email_change_link_is_platform_with_hint(auth_client: Client, run_email_task_inline: MagicMock) -> None:
    response = auth_client.post(
        reverse("api:email-change-request"),
        data=orjson.dumps({"new_email": "new@example.com", "password": PASSWORD}),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    assert response.status_code == 200
    confirmation = next(c for c in run_email_task_inline.call_args_list if c.kwargs["to"] == "new@example.com")
    link = re.findall(rf"{re.escape(SiteSettings.get_solo().frontend_base_url)}/\S+", confirmation.kwargs["body"])[0]
    _assert_platform_link_with_hint(link, "/account/confirm-email-change")
    # The informational notice to the old address carries no link, hence no hint.
    notice = next(c for c in run_email_task_inline.call_args_list if c.kwargs["to"] != "new@example.com")
    assert "return_host" not in notice.kwargs["body"]


def test_delete_request_link_is_platform_with_hint(auth_client: Client, run_email_task_inline: MagicMock) -> None:
    response = auth_client.post(reverse("api:delete-account-request"), HTTP_ORIGIN=TENANT_ORIGIN)

    assert response.status_code == 200
    _assert_platform_link_with_hint(_action_link(run_email_task_inline), "/account/confirm-deletion")


def test_no_origin_means_no_hint(client: Client, user: RevelUser, run_email_task_inline: MagicMock) -> None:
    client.post(
        reverse("api:reset-password-request"), data=orjson.dumps({"email": user.email}), content_type="application/json"
    )

    link = _action_link(run_email_task_inline)
    assert "return_host" not in link


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://app.letsrevel.io", id="spoofed-platform-host"),
        pytest.param("https://unknown.example.com", id="unknown-host"),
        pytest.param(f"http://{HOST}", id="http-without-debug"),
    ],
)
def test_untrusted_origin_means_no_hint(
    client: Client, user: RevelUser, run_email_task_inline: MagicMock, origin: str
) -> None:
    client.post(
        reverse("api:reset-password-request"),
        data=orjson.dumps({"email": user.email}),
        content_type="application/json",
        HTTP_ORIGIN=origin,
    )

    assert "return_host" not in _action_link(run_email_task_inline)


def test_kill_switch_off_means_no_hint(client: Client, user: RevelUser, run_email_task_inline: MagicMock) -> None:
    Organization.objects.update(custom_domain_enabled=False)

    tenant_service.invalidate_tenant_cache(HOST)
    client.post(
        reverse("api:reset-password-request"),
        data=orjson.dumps({"email": user.email}),
        content_type="application/json",
        HTTP_ORIGIN=TENANT_ORIGIN,
    )

    assert "return_host" not in _action_link(run_email_task_inline)


# --- the task itself -------------------------------------------------------------------------


@patch("accounts.tasks.email.send_email")
def test_task_accepts_the_old_positional_signature(mock_send: MagicMock) -> None:
    """Messages queued before this change (no ``return_host``) still run and produce a plain link."""
    send_account_email(AccountEmail.PASSWORD_RESET, "u@example.com", token="tok")  # noqa: S106

    link = _action_link(mock_send)
    assert link == f"{SiteSettings.get_solo().frontend_base_url}/login/reset-password?token=tok"


@patch("accounts.tasks.email.send_email")
def test_task_hint_is_urlencoded_and_never_changes_the_link_host(mock_send: MagicMock) -> None:
    send_account_email(AccountEmail.DELETION, "u@example.com", token="tok", return_host="a.b/c&d=e")  # noqa: S106

    link = _action_link(mock_send)
    assert (
        link
        == f"{SiteSettings.get_solo().frontend_base_url}/account/confirm-deletion?token=tok&return_host=a.b%2Fc%26d%3De"
    )


@patch("accounts.tasks.email.send_email")
def test_task_without_a_link_ignores_the_hint(mock_send: MagicMock) -> None:
    send_account_email(
        AccountEmail.CHANGE_NOTICE, "u@example.com", context={"masked_new_email": "n***@example.com"}, return_host=HOST
    )

    assert "return_host" not in mock_send.call_args.kwargs["body"]
