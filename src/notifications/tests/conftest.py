"""Shared fixtures for notification tests."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery


@pytest.fixture
def user(django_user_model: type[RevelUser]) -> RevelUser:
    """A standard user for testing."""
    return django_user_model.objects.create_user(
        username="testuser@example.com",
        email="testuser@example.com",
        password="strong-password-123!",
        first_name="Test",
        last_name="User",
    )


@pytest.fixture
def regular_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A regular (non-guest) user."""
    return django_user_model.objects.create_user(
        username="regular@example.com",
        email="regular@example.com",
        password="password",
        guest=False,
    )


@pytest.fixture
def guest_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A guest user."""
    return django_user_model.objects.create_user(
        username="guest@example.com",
        email="guest@example.com",
        password="password",
        guest=True,
    )


@pytest.fixture
def notification(regular_user: RevelUser) -> Notification:
    """A sample notification."""
    from notifications.service.dispatcher import create_notification

    return create_notification(
        notification_type=NotificationType.TICKET_CREATED,
        user=regular_user,
        context={
            "ticket_id": str(uuid.uuid4()),
            "ticket_reference": "TKT-001",
            "event_id": str(uuid.uuid4()),
            "event_name": "Test Event",
            "event_start": "2025-12-01T18:00:00Z",
            "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM UTC",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "organization_id": str(uuid.uuid4()),
            "organization_name": "Test Org",
            "tier_name": "General Admission",
            "tier_price": "10.00",
            "ticket_status": "active",
            "quantity": 1,
            "total_price": "10.00",
            "payment_method": "online",
        },
    )


@pytest.fixture
def notification_with_delivery(notification: Notification) -> tuple[Notification, NotificationDelivery]:
    """A notification with a pending delivery."""
    delivery = NotificationDelivery.objects.create(
        notification=notification,
        channel=DeliveryChannel.EMAIL,
        status=DeliveryStatus.PENDING,
    )
    return notification, delivery


@pytest.fixture
def digest_notifications(regular_user: RevelUser) -> list[Notification]:
    """Multiple notifications for digest testing."""
    from notifications.service.dispatcher import create_notification

    base_time = timezone.now() - timedelta(hours=2)

    notifications = []
    for i in range(3):
        event_start = timezone.now() + timedelta(days=i + 1)
        notif = create_notification(
            notification_type=NotificationType.EVENT_REMINDER,
            user=regular_user,
            context={
                "event_id": str(uuid.uuid4()),
                "event_name": f"Event {i + 1}",
                "event_start": event_start.isoformat(),
                "event_start_formatted": f"Event starting in {i + 1} days",
                "event_location": "Test Venue",
                "event_url": f"https://example.com/events/test-{i}",
                "days_until": i + 1,
            },
        )
        # Use QuerySet.update to bypass auto_now_add
        Notification.objects.filter(id=notif.id).update(created_at=base_time + timedelta(minutes=i * 10))
        # Refresh from db to get updated timestamp
        notif.refresh_from_db()
        notifications.append(notif)

    return notifications


@pytest.fixture
def organization(regular_user: RevelUser) -> Organization:
    """A test organization."""
    return Organization.objects.create(
        name="Test Org",
        slug="test-org",
        owner=regular_user,
        accept_membership_requests=True,
    )


@pytest.fixture
def public_event(organization: Organization) -> Event:
    """A public event for testing."""
    next_week = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Public Event",
        slug="public-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        max_attendees=10,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
        accept_invitation_requests=True,
        requires_ticket=True,
    )


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who will be a member."""
    return django_user_model.objects.create_user(
        username="member@example.com",
        email="member@example.com",
        password="pass",
    )


@pytest.fixture
def nonmember_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who is not a member."""
    return django_user_model.objects.create_user(
        username="nonmember@example.com",
        email="nonmember@example.com",
        password="pass",
    )


@pytest.fixture
def ticket_holder(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who holds a ticket."""
    return django_user_model.objects.create_user(
        username="holder@example.com",
        email="holder@example.com",
        password="password",
        first_name="Ticket",
        last_name="Holder",
    )


@pytest.fixture
def ticket_organization(ticket_holder: RevelUser) -> Organization:
    """Organization for ticket tests."""
    return Organization.objects.create(
        name="Ticket Org",
        slug="ticket-org",
        owner=ticket_holder,
    )


@pytest.fixture
def ticket_event(ticket_organization: Organization) -> Event:
    """Event for ticket tests."""
    next_week = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=ticket_organization,
        name="Ticket Event",
        slug="ticket-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        max_attendees=100,
        status="open",
        start=next_week,
        end=next_week + timedelta(hours=3),
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(ticket_event: Event) -> TicketTier:
    """Ticket tier for tests.

    When an event is created with requires_ticket=True, a default tier is
    automatically created via signals. We return that tier instead of
    creating a new one to avoid unique constraint violations.
    """
    return ticket_event.ticket_tiers.first()  # type: ignore[return-value]


@pytest.fixture
def active_ticket(
    ticket_holder: RevelUser,
    ticket_event: Event,
    ticket_tier: TicketTier,
) -> Ticket:
    """An active ticket for testing."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=ticket_holder,
        event=ticket_event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


@pytest.fixture
def pending_ticket(
    ticket_holder: RevelUser,
    ticket_event: Event,
    ticket_tier: TicketTier,
) -> Ticket:
    """A pending ticket for testing."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=ticket_holder,
        event=ticket_event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.PENDING,
    )


def _create_notification_for_test(
    user: RevelUser,
    notification_type: NotificationType,
    context: dict[str, object],
) -> Notification:
    """Create a notification directly without context validation.

    This is for unit testing templates where we only need specific context fields.
    """
    return Notification.objects.create(
        user=user,
        notification_type=notification_type,
        context=context,
    )
