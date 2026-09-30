"""Tests for the RFC 8058 one-click unsubscribe endpoint and token revocation (#1029)."""

import typing as t
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone
from ninja.errors import HttpError

from accounts.jwt import create_token
from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import EmailSuppression, NotificationPreference
from notifications.schema import OneClickUnsubscribePayload, UpdateNotificationPreferenceSchema
from notifications.service.unsubscribe import (
    confirm_unsubscribe,
    generate_email_opt_out_token,
    generate_unsubscribe_token,
)

pytestmark = pytest.mark.django_db

URL = "/api/notification-preferences/one-click"


def _post_form(client: Client, token: str) -> t.Any:
    """Exactly what an RFC 8058 mailbox provider sends."""
    return client.post(
        f"{URL}?token={token}",
        data="List-Unsubscribe=One-Click",
        content_type="application/x-www-form-urlencoded",
    )


def _post_multipart(client: Client, token: str) -> t.Any:
    return client.post(f"{URL}?token={token}", data={"List-Unsubscribe": "One-Click"})


@pytest.fixture
def prefs(regular_user: RevelUser) -> NotificationPreference:
    return regular_user.notification_preferences


class TestOneClickPost:
    @pytest.mark.parametrize("post", [_post_form, _post_multipart])
    def test_org_announcement_mutes_org(
        self,
        post: t.Callable[[Client, str], t.Any],
        client: Client,
        regular_user: RevelUser,
        organization: Organization,
        prefs: NotificationPreference,
    ) -> None:
        token = generate_unsubscribe_token(
            regular_user, notification_type=NotificationType.ORG_ANNOUNCEMENT, organization_id=organization.id
        )

        response = post(client, token)

        assert response.status_code == 200, response.content
        assert list(prefs.muted_organizations.all()) == [organization]
        prefs.refresh_from_db()
        # Muting doesn't touch channel preferences
        assert DeliveryChannel.EMAIL in prefs.get_channels_for_notification_type(NotificationType.EVENT_OPEN)

    def test_other_type_disables_email_for_that_type_only(
        self, client: Client, regular_user: RevelUser, organization: Organization, prefs: NotificationPreference
    ) -> None:
        token = generate_unsubscribe_token(
            regular_user, notification_type=NotificationType.EVENT_OPEN, organization_id=organization.id
        )

        response = _post_form(client, token)

        assert response.status_code == 200
        prefs.refresh_from_db()
        assert DeliveryChannel.EMAIL not in prefs.get_channels_for_notification_type(NotificationType.EVENT_OPEN)
        assert DeliveryChannel.EMAIL in prefs.get_channels_for_notification_type(NotificationType.EVENT_UPDATED)
        assert not prefs.muted_organizations.exists()

    def test_typeless_token_disables_email_and_digest(
        self, client: Client, regular_user: RevelUser, prefs: NotificationPreference
    ) -> None:
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.save()
        token = generate_unsubscribe_token(regular_user)

        response = _post_form(client, token)

        assert response.status_code == 200
        prefs.refresh_from_db()
        assert DeliveryChannel.EMAIL not in prefs.enabled_channels
        assert prefs.digest_frequency == NotificationPreference.DigestFrequency.IMMEDIATE

    def test_email_opt_out_token_suppresses_address(self, client: Client, organization: Organization) -> None:
        token = generate_email_opt_out_token("Invitee@Example.com", organization_id=organization.id)

        response = _post_form(client, token)

        assert response.status_code == 200
        row = EmailSuppression.objects.get()
        assert row.email == "invitee@example.com"
        assert row.reason == EmailSuppression.Reason.INVITATION_OPT_OUT
        assert row.source == EmailSuppression.Source.RECIPIENT
        assert row.organization_id == organization.id

    def test_email_opt_out_for_deleted_org_still_suppresses(self, client: Client, organization: Organization) -> None:
        token = generate_email_opt_out_token("invitee@example.com", organization_id=organization.id)
        organization.delete()

        assert _post_form(client, token).status_code == 200
        assert EmailSuppression.objects.get().organization_id is None

    def test_idempotent(
        self, client: Client, regular_user: RevelUser, organization: Organization, prefs: NotificationPreference
    ) -> None:
        token = generate_unsubscribe_token(
            regular_user, notification_type=NotificationType.ORG_ANNOUNCEMENT, organization_id=organization.id
        )

        assert _post_form(client, token).status_code == 200
        assert _post_form(client, token).status_code == 200
        assert prefs.muted_organizations.count() == 1

    def test_invalid_token_is_400(self, client: Client) -> None:
        assert _post_form(client, "not-a-jwt").status_code == 400

    def test_expired_token_is_400(self, client: Client, regular_user: RevelUser) -> None:
        payload = OneClickUnsubscribePayload(
            user_id=regular_user.id, email=regular_user.email, exp=timezone.now() - timedelta(minutes=1)
        )
        token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        assert _post_form(client, token).status_code == 400

    def test_wrong_token_type_is_400(self, client: Client, regular_user: RevelUser) -> None:
        from accounts.schema import PasswordResetJWTPayloadSchema

        payload = PasswordResetJWTPayloadSchema(
            user_id=regular_user.id, email=regular_user.email, exp=timezone.now() + timedelta(hours=1)
        )
        token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        assert _post_form(client, token).status_code == 400

    def test_unknown_user_is_200(self, client: Client, regular_user: RevelUser) -> None:
        token = generate_unsubscribe_token(regular_user, notification_type=NotificationType.EVENT_OPEN)
        regular_user.delete()

        assert _post_form(client, token).status_code == 200

    def test_token_dies_on_email_change(
        self, client: Client, regular_user: RevelUser, prefs: NotificationPreference
    ) -> None:
        token = generate_unsubscribe_token(regular_user)
        regular_user.email = "new-address@example.com"
        regular_user.save()

        assert _post_form(client, token).status_code == 400
        prefs.refresh_from_db()
        assert DeliveryChannel.EMAIL in prefs.enabled_channels


class TestOneClickGet:
    def test_redirects_to_frontend_without_mutating(
        self, client: Client, regular_user: RevelUser, organization: Organization, prefs: NotificationPreference
    ) -> None:
        token = generate_unsubscribe_token(
            regular_user, notification_type=NotificationType.ORG_ANNOUNCEMENT, organization_id=organization.id
        )
        frontend = SiteSettings.get_solo().frontend_base_url

        response = client.get(f"{URL}?token={token}")

        assert response.status_code == 302
        assert response["Location"] == f"{frontend}/unsubscribe?token={quote(token)}"
        assert not prefs.muted_organizations.exists()

    def test_email_opt_out_get_does_not_suppress(self, client: Client) -> None:
        token = generate_email_opt_out_token("invitee@example.com", organization_id=None)

        assert client.get(f"{URL}?token={token}").status_code == 302
        assert not EmailSuppression.objects.exists()


def test_confirm_unsubscribe_rejects_token_after_email_change(regular_user: RevelUser) -> None:
    token = generate_unsubscribe_token(regular_user)
    regular_user.email = "new-address@example.com"
    regular_user.save()

    with pytest.raises(HttpError) as exc:
        confirm_unsubscribe(token, UpdateNotificationPreferenceSchema(silence_all_notifications=True))  # type: ignore[call-arg]

    assert exc.value.status_code == 400


def test_confirm_unsubscribe_accepts_typed_token(regular_user: RevelUser, organization: Organization) -> None:
    """The footer link carries a typed token; the FE preferences page must keep accepting it."""
    token = generate_unsubscribe_token(
        regular_user, notification_type=NotificationType.ORG_ANNOUNCEMENT, organization_id=organization.id
    )

    prefs = confirm_unsubscribe(token, UpdateNotificationPreferenceSchema(silence_all_notifications=True))  # type: ignore[call-arg]

    assert prefs.silence_all_notifications is True


def test_confirm_unsubscribe_rejects_email_opt_out_token() -> None:
    token = generate_email_opt_out_token("invitee@example.com", organization_id=None)

    with pytest.raises(HttpError):
        confirm_unsubscribe(token, UpdateNotificationPreferenceSchema(silence_all_notifications=True))  # type: ignore[call-arg]
