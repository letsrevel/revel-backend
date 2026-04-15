"""Tests for admin notifications on new user registration (Pushover + Discord)."""

import typing as t
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import override_settings

from accounts.models import Referral, ReferralCode, RevelUser
from accounts.signals import notify_admin_on_user_creation
from accounts.tasks import (
    notify_admin_new_user_joined,
    notify_admin_new_user_joined_discord,
)
from common.models import SiteSettings


@pytest.fixture
def enable_user_notifications() -> t.Iterator[None]:
    """Set SiteSettings.notify_user_joined = True."""
    settings = SiteSettings.get_solo()
    original = settings.notify_user_joined
    settings.notify_user_joined = True
    settings.save()
    try:
        yield
    finally:
        settings.notify_user_joined = original
        settings.save()


def _make_response(status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.raise_for_status.return_value = None
    return response


@pytest.mark.django_db
@override_settings(PUSHOVER_USER_KEY="", PUSHOVER_APP_TOKEN="")
def test_pushover_skipped_when_not_configured(user: RevelUser) -> None:
    with patch("accounts.tasks.httpx.post") as mock_post:
        result = notify_admin_new_user_joined(user_id=str(user.id), user_email=user.email, is_guest=False)
    assert result == {"status": "skipped", "reason": "pushover_not_configured"}
    mock_post.assert_not_called()


@pytest.mark.django_db
@override_settings(PUSHOVER_USER_KEY="key", PUSHOVER_APP_TOKEN="token")
def test_pushover_message_without_referrer_has_user_count(user: RevelUser) -> None:
    with patch("accounts.tasks.httpx.post", return_value=_make_response()) as mock_post:
        notify_admin_new_user_joined(user_id=str(user.id), user_email=user.email, is_guest=False)

    _, kwargs = mock_post.call_args
    message = kwargs["data"]["message"]
    assert user.email in message
    assert "Referred by" not in message
    total_non_guest_users = RevelUser.objects.filter(guest=False).count()
    assert f"We now have {total_non_guest_users} users!" in message


@pytest.mark.django_db
@override_settings(PUSHOVER_USER_KEY="key", PUSHOVER_APP_TOKEN="token")
def test_pushover_message_includes_referrer_when_present(user: RevelUser, django_user_model: type[RevelUser]) -> None:
    referrer = django_user_model.objects.create_user(
        username="referrer@example.com", email="referrer@example.com", password="x"
    )
    code = ReferralCode.objects.create(user=referrer, code="ABCD1234")
    Referral.objects.create(referral_code=code, referred_user=user)

    with patch("accounts.tasks.httpx.post", return_value=_make_response()) as mock_post:
        notify_admin_new_user_joined(user_id=str(user.id), user_email=user.email, is_guest=False)

    message = mock_post.call_args.kwargs["data"]["message"]
    assert f"Referred by: {referrer.email}" in message


@pytest.mark.django_db
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="")
def test_discord_skipped_when_not_configured() -> None:
    with patch("accounts.tasks.httpx.post") as mock_post:
        result = notify_admin_new_user_joined_discord()
    assert result == {"status": "skipped", "reason": "discord_webhook_not_configured"}
    mock_post.assert_not_called()


@pytest.mark.django_db
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="https://example.com/webhook")
def test_discord_message_omits_pii_and_includes_count(user: RevelUser) -> None:
    with patch("accounts.tasks.httpx.post", return_value=_make_response()) as mock_post:
        notify_admin_new_user_joined_discord()

    _, kwargs = mock_post.call_args
    content = kwargs["json"]["content"]
    assert user.email not in content
    assert user.first_name not in content
    assert str(user.id) not in content
    total_non_guest_users = RevelUser.objects.filter(guest=False).count()
    assert f"We now have {total_non_guest_users} users." in content


@pytest.mark.django_db
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="https://example.com/webhook")
def test_discord_retries_on_http_error() -> None:
    mock_request = MagicMock()
    err = httpx.HTTPStatusError("boom", request=mock_request, response=MagicMock(status_code=500))
    with (
        patch("accounts.tasks.httpx.post", side_effect=err),
        patch.object(notify_admin_new_user_joined_discord, "retry", side_effect=RuntimeError("retried")) as mock_retry,
        pytest.raises(RuntimeError, match="retried"),
    ):
        notify_admin_new_user_joined_discord()
    mock_retry.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_signal_dispatches_both_tasks_on_commit_when_enabled(
    django_user_model: type[RevelUser], enable_user_notifications: None
) -> None:
    with (
        patch("accounts.signals.notify_admin_new_user_joined.delay") as mock_pushover,
        patch("accounts.signals.notify_admin_new_user_joined_discord.delay") as mock_discord,
    ):
        django_user_model.objects.create_user(
            username="dispatchtest@example.com",
            email="dispatchtest@example.com",
            password="x",
        )
    mock_pushover.assert_called_once()
    mock_discord.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_signal_does_not_dispatch_when_disabled(django_user_model: type[RevelUser]) -> None:
    settings = SiteSettings.get_solo()
    settings.notify_user_joined = False
    settings.save()

    with (
        patch("accounts.signals.notify_admin_new_user_joined.delay") as mock_pushover,
        patch("accounts.signals.notify_admin_new_user_joined_discord.delay") as mock_discord,
    ):
        django_user_model.objects.create_user(username="skipped@example.com", email="skipped@example.com", password="x")
    mock_pushover.assert_not_called()
    mock_discord.assert_not_called()


@pytest.mark.django_db
def test_signal_handler_noop_on_update(user: RevelUser) -> None:
    """notify_admin_on_user_creation should early-return when created=False."""
    with (
        patch("accounts.signals.notify_admin_new_user_joined.delay") as mock_pushover,
        patch("accounts.signals.notify_admin_new_user_joined_discord.delay") as mock_discord,
    ):
        notify_admin_on_user_creation(sender=RevelUser, instance=user, created=False)
    mock_pushover.assert_not_called()
    mock_discord.assert_not_called()
