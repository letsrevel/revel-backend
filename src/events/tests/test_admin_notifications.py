"""Tests for admin notifications on new organization creation (Pushover + Discord)."""

import typing as t
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import override_settings

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import MembershipTier, Organization
from events.tasks import (
    notify_admin_new_organization_discord,
    notify_admin_new_organization_pushover,
)


@pytest.fixture
def enable_org_notifications() -> t.Iterator[None]:
    settings = SiteSettings.get_solo()
    original = settings.notify_organization_created
    settings.notify_organization_created = True
    settings.save()
    try:
        yield
    finally:
        settings.notify_organization_created = original
        settings.save()


def _make_response(status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.raise_for_status.return_value = None
    return response


@pytest.mark.django_db
@override_settings(PUSHOVER_USER_KEY="", PUSHOVER_APP_TOKEN="")
def test_org_pushover_skipped_when_not_configured(organization: Organization) -> None:
    with patch("events.tasks.httpx.post") as mock_post:
        result = notify_admin_new_organization_pushover(organization_id=str(organization.id))
    assert result == {"status": "skipped", "reason": "pushover_not_configured"}
    mock_post.assert_not_called()


@pytest.mark.django_db
@override_settings(PUSHOVER_USER_KEY="key", PUSHOVER_APP_TOKEN="token")
def test_org_pushover_includes_name_owner_and_count(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    with patch("events.tasks.httpx.post", return_value=_make_response()) as mock_post:
        notify_admin_new_organization_pushover(organization_id=str(organization.id))

    message = mock_post.call_args.kwargs["data"]["message"]
    assert organization.name in message
    assert organization_owner_user.email in message
    org_count = Organization.objects.count()
    assert f"We now have {org_count} organizations!" in message


@pytest.mark.django_db
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="")
def test_org_discord_skipped_when_not_configured(organization: Organization) -> None:
    with patch("events.tasks.httpx.post") as mock_post:
        result = notify_admin_new_organization_discord(organization_id=str(organization.id))
    assert result == {"status": "skipped", "reason": "discord_webhook_not_configured"}
    mock_post.assert_not_called()


@pytest.mark.django_db
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="https://example.com/webhook")
def test_org_discord_includes_name_owner_email_and_count(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    with patch("events.tasks.httpx.post", return_value=_make_response()) as mock_post:
        notify_admin_new_organization_discord(organization_id=str(organization.id))

    payload = mock_post.call_args.kwargs["json"]
    content = payload["content"]
    assert organization.name in content
    assert organization_owner_user.email in content
    org_count = Organization.objects.count()
    assert f"We now have {org_count} organizations." in content
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.django_db(transaction=True)
def test_org_signal_dispatches_both_tasks_when_enabled(
    organization_owner_user: RevelUser, enable_org_notifications: None
) -> None:
    with (
        patch("events.signals.notify_admin_new_organization_pushover.delay") as mock_pushover,
        patch("events.signals.notify_admin_new_organization_discord.delay") as mock_discord,
    ):
        org = Organization.objects.create(name="NotifOrg", slug="notiforg", owner=organization_owner_user)
    mock_pushover.assert_called_once_with(organization_id=str(org.id))
    mock_discord.assert_called_once_with(organization_id=str(org.id))
    # Default membership tier must still be created
    assert MembershipTier.objects.filter(organization=org, name="General membership").exists()


@pytest.mark.django_db(transaction=True)
def test_org_signal_does_not_dispatch_when_disabled(organization_owner_user: RevelUser) -> None:
    settings = SiteSettings.get_solo()
    settings.notify_organization_created = False
    settings.save()

    with (
        patch("events.signals.notify_admin_new_organization_pushover.delay") as mock_pushover,
        patch("events.signals.notify_admin_new_organization_discord.delay") as mock_discord,
    ):
        org = Organization.objects.create(name="QuietOrg", slug="quietorg", owner=organization_owner_user)
    mock_pushover.assert_not_called()
    mock_discord.assert_not_called()
    # MembershipTier still gets created regardless
    assert MembershipTier.objects.filter(organization=org, name="General membership").exists()
