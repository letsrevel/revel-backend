"""Keyboard builders for notification messages sent via Telegram."""

import typing as t

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from events.service.event_manager import EligibilityService
from notifications.enums import NotificationType
from notifications.models import Notification
from telegram.keyboards import get_event_eligible_keyboard


def get_notification_keyboard(notification: Notification) -> InlineKeyboardMarkup | None:
    """Build appropriate inline keyboard for notification based on type.

    Args:
        notification: The notification to build keyboard for

    Returns:
        InlineKeyboardMarkup or None if no keyboard needed
    """
    notification_type = notification.notification_type
    context = notification.context
    user = notification.user

    # Delegate to specific keyboard builders based on notification type
    if _is_event_notification_with_eligibility(notification_type):
        return _build_event_keyboard_from_context(context, user)

    if _is_organizer_action_notification(notification_type):
        return _build_organizer_action_keyboard(notification_type, context)

    if _is_simple_event_link_notification(notification_type):
        event_id = context.get("event_id")
        return _get_simple_event_link(event_id) if event_id else None

    if _is_membership_notification(notification_type):
        org_id = context.get("organization_id")
        return _get_organization_link(org_id) if org_id else None

    return None


def _is_event_notification_with_eligibility(notification_type: str) -> bool:
    """Check if notification type requires event keyboard with eligibility."""
    return notification_type in (
        NotificationType.EVENT_OPEN,
        NotificationType.EVENT_UPDATED,
        NotificationType.EVENT_REMINDER,
        NotificationType.INVITATION_RECEIVED,
        NotificationType.WAITLIST_SPOT_AVAILABLE,
    )


def _is_organizer_action_notification(notification_type: str) -> bool:
    """Check if notification type is for organizer actions."""
    return notification_type in (
        NotificationType.INVITATION_REQUEST_CREATED,
        NotificationType.MEMBERSHIP_REQUEST_CREATED,
    )


def _is_simple_event_link_notification(notification_type: str) -> bool:
    """Check if notification type needs simple event link."""
    return notification_type in (
        NotificationType.TICKET_CREATED,
        NotificationType.TICKET_UPDATED,
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        NotificationType.TICKET_CHECKED_IN,
        NotificationType.PAYMENT_CONFIRMATION,
        NotificationType.RSVP_CONFIRMATION,
        NotificationType.RSVP_UPDATED,
        NotificationType.RSVP_CANCELLED,
    )


def _is_membership_notification(notification_type: str) -> bool:
    """Check if notification type is membership-related."""
    return notification_type in (
        NotificationType.MEMBERSHIP_GRANTED,
        NotificationType.MEMBERSHIP_PROMOTED,
        NotificationType.MEMBERSHIP_REMOVED,
        NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        NotificationType.MEMBERSHIP_REQUEST_REJECTED,
    )


def _build_event_keyboard_from_context(context: dict[str, t.Any], user: RevelUser) -> InlineKeyboardMarkup | None:
    """Build event keyboard from notification context."""
    event_id = context.get("event_id")
    return _get_event_keyboard(event_id, user) if event_id else None


def _build_organizer_action_keyboard(notification_type: str, context: dict[str, t.Any]) -> InlineKeyboardMarkup | None:
    """Build accept/reject keyboard for organizer actions."""
    request_id = context.get("request_id")
    if not request_id:
        return None

    if notification_type == NotificationType.INVITATION_REQUEST_CREATED:
        return _get_invitation_request_keyboard(request_id)

    if notification_type == NotificationType.MEMBERSHIP_REQUEST_CREATED:
        return _get_membership_request_keyboard(request_id)

    return None


def _get_event_keyboard(event_id: str, user: RevelUser) -> InlineKeyboardMarkup:
    """Build keyboard with eligibility-aware actions for event.

    Uses the existing EventKeyboardHandler from keyboards.py to avoid duplication.

    Args:
        event_id: Event UUID
        user: User receiving notification

    Returns:
        InlineKeyboardMarkup with appropriate actions
    """
    event = Event.objects.select_related("organization").prefetch_related("ticket_tiers").get(pk=event_id)
    eligibility_service = EligibilityService(user=user, event=event)
    eligibility = eligibility_service.check_eligibility()

    # Reuse existing keyboard handler from keyboards.py
    return get_event_eligible_keyboard(event=event, eligibility=eligibility, user=user)


def _get_invitation_request_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Build Accept/Reject keyboard for invitation requests.

    Args:
        request_id: InvitationRequest UUID

    Returns:
        InlineKeyboardMarkup with Accept/Reject buttons
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Accept", callback_data=f"invitation_request_accept:{request_id}")
    builder.button(text="❌ Reject", callback_data=f"invitation_request_reject:{request_id}")
    builder.adjust(2)  # Two buttons in one row
    return builder.as_markup()


def _get_membership_request_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Build Accept/Reject keyboard for membership requests.

    Args:
        request_id: OrganizationMembershipRequest UUID

    Returns:
        InlineKeyboardMarkup with Accept/Reject buttons
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve", callback_data=f"membership_request_approve:{request_id}")
    builder.button(text="❌ Reject", callback_data=f"membership_request_reject:{request_id}")
    builder.adjust(2)  # Two buttons in one row
    return builder.as_markup()


def _get_simple_event_link(event_id: str) -> InlineKeyboardMarkup:
    """Build simple event link button.

    Args:
        event_id: Event UUID

    Returns:
        InlineKeyboardMarkup with single event link
    """
    event = Event.objects.only("id").get(pk=event_id)
    site_settings = SiteSettings.get_solo()
    frontend_url = f"{site_settings.frontend_base_url}/events/{event.id}"

    builder = InlineKeyboardBuilder()
    builder.button(text="📅 View Event", url=frontend_url)
    return builder.as_markup()


def _get_organization_link(org_id: str) -> InlineKeyboardMarkup:
    """Build simple organization link button.

    Args:
        org_id: Organization UUID

    Returns:
        InlineKeyboardMarkup with single organization link
    """
    org = Organization.objects.only("id", "slug").get(pk=org_id)
    site_settings = SiteSettings.get_solo()
    frontend_url = f"{site_settings.frontend_base_url}/organizations/{org.slug}"

    builder = InlineKeyboardBuilder()
    builder.button(text="🏢 View Organization", url=frontend_url)
    return builder.as_markup()
