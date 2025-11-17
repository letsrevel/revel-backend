"""Shared fixtures for notification tests."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
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
