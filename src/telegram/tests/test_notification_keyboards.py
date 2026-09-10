"""Tests for the Telegram inline keyboards attached to notifications.

``notifications/tests/test_channels.py`` stubs ``get_notification_keyboard`` out, so
the real builders were never executed. This module calls the real function once per
predicate family and round-trips the callback data the routers parse back.
"""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    Event,
    EventInvitationRequest,
    Organization,
    OrganizationMembershipRequest,
)
from notifications.enums import NotificationType
from notifications.models import Notification
from telegram.notification_keyboards import get_notification_keyboard

pytestmark = pytest.mark.django_db


@pytest.fixture
def notified_user(django_user_model: type[RevelUser]) -> RevelUser:
    """The recipient of the notifications built below."""
    return django_user_model.objects.create_user(username="kb@example.com", email="kb@example.com", password="pw")


@pytest.fixture
def kb_organization(notified_user: RevelUser) -> Organization:
    """Organization owning the event/requests below."""
    return Organization.objects.create(name="KB Org", slug="kb-org", owner=notified_user)


@pytest.fixture
def kb_event(kb_organization: Organization) -> Event:
    """A public, ticketed, future event."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=kb_organization,
        name="KB Event",
        slug="kb-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        max_attendees=10,
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )


def _notify(user: RevelUser, notification_type: NotificationType, context: dict[str, t.Any]) -> Notification:
    """Create a notification without going through context validation."""
    return Notification.objects.create(user=user, notification_type=notification_type, context=context)


def _callback_data(markup: t.Any) -> list[str | None]:
    """Flatten a markup's buttons into their callback_data values."""
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _urls(markup: t.Any) -> list[str | None]:
    """Flatten a markup's buttons into their url values."""
    return [button.url for row in markup.inline_keyboard for button in row]


# --- Event notifications with eligibility ---


@pytest.mark.parametrize(
    "notification_type",
    [
        NotificationType.EVENT_OPEN,
        NotificationType.EVENT_UPDATED,
        NotificationType.EVENT_REMINDER,
        NotificationType.INVITATION_RECEIVED,
        NotificationType.WAITLIST_SPOT_AVAILABLE,
    ],
)
def test_event_notifications_get_an_eligibility_keyboard(
    notified_user: RevelUser, kb_event: Event, notification_type: NotificationType
) -> None:
    """Event-scoped types run the real eligibility check and return a keyboard."""
    notification = _notify(notified_user, notification_type, {"event_id": str(kb_event.pk)})

    markup = get_notification_keyboard(notification)

    assert markup is not None
    assert markup.inline_keyboard


def test_event_notification_without_event_id_has_no_keyboard(notified_user: RevelUser) -> None:
    """A missing event_id short-circuits before hitting the database."""
    notification = _notify(notified_user, NotificationType.EVENT_OPEN, {"event_name": "No id"})

    assert get_notification_keyboard(notification) is None


# --- Organizer action notifications ---


def test_invitation_request_keyboard_round_trips_callback_data(notified_user: RevelUser, kb_event: Event) -> None:
    """Accept/Reject buttons carry the callback data the invitation router parses."""
    request = EventInvitationRequest.objects.create(event=kb_event, user=notified_user)
    notification = _notify(
        notified_user,
        NotificationType.INVITATION_REQUEST_CREATED,
        {"request_id": str(request.pk), "event_name": kb_event.name},
    )

    markup = get_notification_keyboard(notification)

    assert markup is not None
    assert _callback_data(markup) == [
        f"invitation_request_accept:{request.pk}",
        f"invitation_request_reject:{request.pk}",
    ]


def test_membership_request_keyboard_links_to_the_members_admin(
    notified_user: RevelUser, kb_organization: Organization
) -> None:
    """The membership request keyboard is a single deep link to the members admin."""
    request = OrganizationMembershipRequest.objects.create(organization=kb_organization, user=notified_user)
    notification = _notify(
        notified_user,
        NotificationType.MEMBERSHIP_REQUEST_CREATED,
        {"request_id": str(request.pk), "organization_name": kb_organization.name},
    )

    markup = get_notification_keyboard(notification)

    assert markup is not None
    base_url = SiteSettings.get_solo().frontend_base_url
    assert _urls(markup) == [f"{base_url}/org/{kb_organization.slug}/admin/members"]


def test_whitelist_request_keyboard_round_trips_callback_data(notified_user: RevelUser) -> None:
    """Approve/Reject buttons carry the callback data the whitelist router parses."""
    request_id = "6f1a3f6e-0f0a-4a0e-9c5f-9b2d0f1a3f6e"
    notification = _notify(
        notified_user,
        NotificationType.WHITELIST_REQUEST_CREATED,
        {"request_id": request_id, "organization_name": "KB Org"},
    )

    markup = get_notification_keyboard(notification)

    assert markup is not None
    assert _callback_data(markup) == [
        f"whitelist_request_approve:{request_id}",
        f"whitelist_request_reject:{request_id}",
    ]


def test_organizer_action_without_request_id_has_no_keyboard(notified_user: RevelUser) -> None:
    """A missing request_id short-circuits before hitting the database."""
    notification = _notify(notified_user, NotificationType.MEMBERSHIP_REQUEST_CREATED, {"organization_name": "KB Org"})

    assert get_notification_keyboard(notification) is None


# --- Simple event link notifications ---


def test_ticket_notification_gets_a_simple_event_link(notified_user: RevelUser, kb_event: Event) -> None:
    """Ticket/RSVP types get a single "View Event" link."""
    notification = _notify(notified_user, NotificationType.TICKET_CREATED, {"event_id": str(kb_event.pk)})

    markup = get_notification_keyboard(notification)

    assert markup is not None
    base_url = SiteSettings.get_solo().frontend_base_url
    assert _urls(markup) == [f"{base_url}/events/{kb_event.pk}"]


def test_ticket_notification_without_event_id_has_no_keyboard(notified_user: RevelUser) -> None:
    """A missing event_id short-circuits before hitting the database."""
    notification = _notify(notified_user, NotificationType.RSVP_CONFIRMATION, {"event_name": "No id"})

    assert get_notification_keyboard(notification) is None


# --- Membership notifications ---


def test_membership_notification_gets_an_organization_link(
    notified_user: RevelUser, kb_organization: Organization
) -> None:
    """Membership types get a single "View Organization" link."""
    notification = _notify(
        notified_user,
        NotificationType.MEMBERSHIP_GRANTED,
        {"organization_id": str(kb_organization.pk)},
    )

    markup = get_notification_keyboard(notification)

    assert markup is not None
    base_url = SiteSettings.get_solo().frontend_base_url
    assert _urls(markup) == [f"{base_url}/org/{kb_organization.slug}"]


def test_membership_notification_without_organization_id_has_no_keyboard(notified_user: RevelUser) -> None:
    """A missing organization_id short-circuits before hitting the database."""
    notification = _notify(notified_user, NotificationType.MEMBERSHIP_REMOVED, {"organization_name": "KB Org"})

    assert get_notification_keyboard(notification) is None


# --- Types with no keyboard at all ---


@pytest.mark.parametrize(
    "notification_type",
    [NotificationType.SYSTEM_ANNOUNCEMENT, NotificationType.ACCOUNT_BANNED],
)
def test_unmapped_notification_types_have_no_keyboard(
    notified_user: RevelUser, notification_type: NotificationType
) -> None:
    """Types outside every predicate family fall through to None."""
    notification = _notify(notified_user, notification_type, {"message": "hi"})

    assert get_notification_keyboard(notification) is None
