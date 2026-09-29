"""Service layer for notification unsubscribe functionality."""

from uuid import UUID

import jwt
import structlog
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from accounts.service.account import token_to_payload
from events.models import Organization
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import EmailSuppression, NotificationPreference
from notifications.schema import (
    EmailOptOutJWTPayloadSchema,
    OneClickUnsubscribePayload,
    UpdateNotificationPreferenceSchema,
)
from notifications.service.email_policy import suppress

logger = structlog.get_logger(__name__)


def generate_unsubscribe_token(
    user: RevelUser,
    notification_type: str | None = None,
    organization_id: UUID | None = None,
) -> str:
    """Generate an unsubscribe token for a user.

    Args:
        user: The user to generate the token for
        notification_type: Type of the email carrying the token; scopes the one-click
            effect to that type. None (digest) means "stop email altogether".
        organization_id: Sending organization; with ORG_ANNOUNCEMENT, one-click mutes it.

    Returns:
        The unsubscribe token
    """
    payload = OneClickUnsubscribePayload(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME,
        notification_type=NotificationType(notification_type) if notification_type else None,
        organization_id=organization_id,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.debug("unsubscribe_token_generated", user_id=str(user.id))
    return token


def generate_email_opt_out_token(email: str, organization_id: UUID | None) -> str:
    """Generate an opt-out token for cold mail (pending invitations) to a non-user address.

    Args:
        email: Recipient address.
        organization_id: Organization whose invitation carries the token.

    Returns:
        The opt-out token.
    """
    payload = EmailOptOutJWTPayloadSchema(
        email=email,
        organization_id=organization_id,
        exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME,
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


def _reject_if_email_changed(payload: OneClickUnsubscribePayload, user: RevelUser) -> None:
    """Unsubscribe tokens die when the account's address changes."""
    if payload.email.lower() != user.email.lower():
        raise HttpError(400, str(_("This unsubscribe link is no longer valid.")))


def confirm_unsubscribe(token: str, preferences: UpdateNotificationPreferenceSchema) -> NotificationPreference:
    """Confirm and execute notification preference update via unsubscribe token.

    Only the submitted global fields are updated. Per-type overrides are left alone:
    pinning the submitted channels onto every type used to make a later global
    re-enable ineffective and re-routed types with their own defaults (#1030).
    Silence and the email switch don't need the sync: they are honoured by
    ``get_channels_for_notification_type``, and mandatory types bypass them.

    Args:
        token: The unsubscribe token
        preferences: The notification preferences to update

    Returns:
        The updated notification preferences
    """
    payload = token_to_payload(token, OneClickUnsubscribePayload)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)
    _reject_if_email_changed(payload, user)

    logger.info("unsubscribe_confirmed", user_id=str(user.id), email=user.email)

    # Get or create notification preferences
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)

    # Update preferences using the same logic as the authenticated endpoint
    update_data = preferences.model_dump(exclude_unset=True)

    if not update_data:
        return prefs

    for field, value in update_data.items():
        setattr(prefs, field, value)

    prefs.save(update_fields=list(update_data.keys()) + ["updated_at"])

    logger.info(
        "unsubscribe_preferences_updated",
        user_id=str(user.id),
        email=user.email,
        updated_fields=list(update_data.keys()),
    )

    return prefs


def _token_type(token: str) -> str | None:
    """Peek at the unverified ``type`` claim; the caller verifies via ``token_to_payload``."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    token_type = claims.get("type")
    return token_type if isinstance(token_type, str) else None


def one_click_unsubscribe(token: str) -> None:
    """Apply an RFC 8058 one-click unsubscribe. Idempotent.

    - ``email_opt_out`` → suppress the address for cold invitations.
    - ``unsubscribe`` + ORG_ANNOUNCEMENT + organization → mute that organization.
    - ``unsubscribe`` + another type → stop emailing that type.
    - ``unsubscribe`` without a type (digest) → stop email and switch the digest off.

    Args:
        token: Token from the List-Unsubscribe URL.

    Raises:
        HttpError: 400 if the token is invalid, expired, or its address is stale.
    """
    if _token_type(token) == "email_opt_out":
        opt_out = token_to_payload(token, EmailOptOutJWTPayloadSchema)
        org_id = opt_out.organization_id
        if org_id and not Organization.objects.filter(pk=org_id).exists():
            org_id = None
        suppress(
            opt_out.email,
            EmailSuppression.Reason.INVITATION_OPT_OUT,
            EmailSuppression.Source.RECIPIENT,
            organization_id=org_id,
        )
        logger.info("one_click_invitation_opt_out", organization_id=str(org_id) if org_id else None)
        return

    payload = token_to_payload(token, OneClickUnsubscribePayload)
    check_blacklist(payload.jti)
    user = RevelUser.objects.filter(pk=payload.user_id).first()
    if user is None:
        logger.info("one_click_unsubscribe_unknown_user", user_id=str(payload.user_id))
        return
    _reject_if_email_changed(payload, user)
    prefs, _created = NotificationPreference.objects.get_or_create(user=user)

    if payload.notification_type == NotificationType.ORG_ANNOUNCEMENT and payload.organization_id:
        prefs.muted_organizations.add(*Organization.objects.filter(pk=payload.organization_id))
    elif payload.notification_type:
        if prefs.disable_email_for_type(payload.notification_type):
            prefs.save(update_fields=["notification_type_settings", "updated_at"])
    else:
        prefs.disable_channel(DeliveryChannel.EMAIL)
        prefs.digest_frequency = NotificationPreference.DigestFrequency.IMMEDIATE
        prefs.save(update_fields=["enabled_channels", "notification_type_settings", "digest_frequency", "updated_at"])

    logger.info(
        "one_click_unsubscribe",
        user_id=str(user.id),
        notification_type=payload.notification_type,
        organization_id=str(payload.organization_id) if payload.organization_id else None,
    )
