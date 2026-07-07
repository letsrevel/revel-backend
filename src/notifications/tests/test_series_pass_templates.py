"""Tests for series pass purchased/extended notifications (#644, Task 9).

Covers:
- Free-pass purchase sends exactly one SERIES_PASS_PURCHASED to the holder and to
  eligible org staff/owners.
- Stripe webhook activation of an online pass sends the same.
- Offline (PENDING) purchase sends nothing until confirmation (Task 15 endpoint).
- Template rendering for both SERIES_PASS_PURCHASED and SERIES_PASS_EXTENDED.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template, is_template_registered
from notifications.service.templates.series_pass_templates import (
    SeriesPassExtendedTemplate,
    SeriesPassPurchasedTemplate,
)
from notifications.signals.series_pass import send_series_pass_extended

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def holder(revel_user_factory: RevelUserFactory) -> RevelUser:
    """The user purchasing the series pass."""
    return revel_user_factory(username="pass_holder@example.com", email="pass_holder@example.com")


@pytest.fixture
def event_series(organization: Organization) -> EventSeries:
    """Series the passes below belong to."""
    return EventSeries.objects.create(organization=organization, name="Weekly Classes", slug="weekly-classes")


def _make_future_event_and_tier(
    organization: Organization,
    event_series: EventSeries,
    name: str,
    slug: str,
    days: int,
    payment_method: str,
) -> tuple[Event, TicketTier]:
    """Create a future, open, ticketed event with one tier of the given payment method."""
    event = Event.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=days),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier for {name}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=payment_method,
    )
    return event, tier


def _make_series_pass(
    organization: Organization,
    event_series: EventSeries,
    *,
    name: str,
    payment_method: str,
    price: Decimal,
) -> SeriesPass:
    """Create a series pass covering 2 future events of the given payment method."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name=name,
        price=price,
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=payment_method,
    )
    for i in range(2):
        event, tier = _make_future_event_and_tier(
            organization,
            event_series,
            f"{name} Event {i}",
            f"{name.lower().replace(' ', '-')}-event-{i}",
            i + 1,
            payment_method,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def free_series_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """A FREE series pass covering 2 future events."""
    return _make_series_pass(
        organization,
        event_series,
        name="Free Season Pass",
        payment_method=TicketTier.PaymentMethod.FREE,
        price=Decimal("0.00"),
    )


@pytest.fixture
def offline_series_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """An OFFLINE series pass covering 2 future events."""
    return _make_series_pass(
        organization,
        event_series,
        name="Offline Season Pass",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price=Decimal("20.00"),
    )


@pytest.fixture
def online_series_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """An ONLINE series pass covering 2 future events."""
    return _make_series_pass(
        organization,
        event_series,
        name="Online Season Pass",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=Decimal("20.00"),
    )


@pytest.fixture
def staff_member(organization: Organization, revel_user_factory: RevelUserFactory) -> RevelUser:
    """A staff member with default (manage_tickets-enabled) permissions."""
    staff_user = revel_user_factory(username="staff@example.com", email="staff@example.com")
    OrganizationStaff.objects.create(organization=organization, user=staff_user)
    return staff_user


def _completed_checkout_event(session_id: str) -> MagicMock:
    """Build a fake, iterable ``checkout.session.completed`` stripe.Event."""
    session_data = {"id": session_id, "payment_status": "paid", "payment_intent": f"pi_{session_id}"}
    event_data = {"type": "checkout.session.completed", "data": {"object": session_data}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = session_data
    return mock_event


# --- Dispatch wiring tests ---


class TestFreePassPurchaseNotifications:
    """Free-pass purchase sends exactly one SERIES_PASS_PURCHASED to holder + staff/owner."""

    def test_sends_to_holder_and_owner_and_staff(
        self,
        free_series_pass: SeriesPass,
        holder: RevelUser,
        organization: Organization,
        staff_member: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            SeriesPassPurchaseService(free_series_pass, holder).purchase()

        held_pass = HeldSeriesPass.objects.get(series_pass=free_series_pass, user=holder)
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE

        holder_notifications = list(
            Notification.objects.filter(user=holder, notification_type=NotificationType.SERIES_PASS_PURCHASED)
        )
        assert len(holder_notifications) == 1
        assert holder_notifications[0].context["pass_name"] == "Free Season Pass"
        assert holder_notifications[0].context["event_count"] == 2

        owner_notifications = Notification.objects.filter(
            user=organization.owner, notification_type=NotificationType.SERIES_PASS_PURCHASED
        )
        assert owner_notifications.count() == 1

        staff_notifications = list(
            Notification.objects.filter(user=staff_member, notification_type=NotificationType.SERIES_PASS_PURCHASED)
        )
        assert len(staff_notifications) == 1
        assert staff_notifications[0].context["holder_email"] == holder.email


class TestWebhookActivationNotifications:
    """Webhook activation of an online pass sends SERIES_PASS_PURCHASED."""

    def test_activation_sends_purchased_notification(
        self,
        online_series_pass: SeriesPass,
        holder: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        organization.stripe_account_id = "acct_test123"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save()

        session_id = "cs_series_pass_notif"
        mock_session = Mock()
        mock_session.id = session_id
        mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
        with patch("stripe.checkout.Session.create", return_value=mock_session):
            SeriesPassPurchaseService(online_series_pass, holder).purchase()

        held_pass = HeldSeriesPass.objects.get(series_pass=online_series_pass, user=holder)
        assert held_pass.status == HeldSeriesPass.Status.PENDING
        assert not Notification.objects.filter(
            user=holder, notification_type=NotificationType.SERIES_PASS_PURCHASED
        ).exists()

        event = _completed_checkout_event(session_id)
        with django_capture_on_commit_callbacks(execute=True):
            StripeEventHandler(event).handle_checkout_session_completed(event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE

        holder_notifications = Notification.objects.filter(
            user=holder, notification_type=NotificationType.SERIES_PASS_PURCHASED
        )
        assert holder_notifications.count() == 1

    def test_duplicate_delivery_sends_only_one_notification(
        self,
        online_series_pass: SeriesPass,
        holder: RevelUser,
        organization: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A re-delivered webhook must not fire the notification a second time."""
        organization.stripe_account_id = "acct_test123"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save()

        session_id = "cs_series_pass_dup"
        mock_session = Mock()
        mock_session.id = session_id
        mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
        with patch("stripe.checkout.Session.create", return_value=mock_session):
            SeriesPassPurchaseService(online_series_pass, holder).purchase()

        with django_capture_on_commit_callbacks(execute=True):
            StripeEventHandler(_completed_checkout_event(session_id)).handle_checkout_session_completed(
                _completed_checkout_event(session_id)
            )
        with django_capture_on_commit_callbacks(execute=True):
            StripeEventHandler(_completed_checkout_event(session_id)).handle_checkout_session_completed(
                _completed_checkout_event(session_id)
            )

        assert (
            Notification.objects.filter(user=holder, notification_type=NotificationType.SERIES_PASS_PURCHASED).count()
            == 1
        )


class TestOfflinePurchaseSendsNothing:
    """Offline (PENDING) purchase must not fire SERIES_PASS_PURCHASED until confirmed."""

    def test_offline_purchase_sends_no_notification(
        self,
        offline_series_pass: SeriesPass,
        holder: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            SeriesPassPurchaseService(offline_series_pass, holder).purchase()

        held_pass = HeldSeriesPass.objects.get(series_pass=offline_series_pass, user=holder)
        assert held_pass.status == HeldSeriesPass.Status.PENDING

        assert not Notification.objects.filter(
            user=holder, notification_type=NotificationType.SERIES_PASS_PURCHASED
        ).exists()


class TestSendSeriesPassExtendedHelper:
    """Direct unit test of the dispatch helper Task 10's Celery task will call."""

    def test_sends_extended_notification_to_holder(
        self,
        free_series_pass: SeriesPass,
        holder: RevelUser,
        organization: Organization,
        event_series: EventSeries,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            SeriesPassPurchaseService(free_series_pass, holder).purchase()
        held_pass = HeldSeriesPass.objects.get(series_pass=free_series_pass, user=holder)

        new_event, _new_tier = _make_future_event_and_tier(
            organization, event_series, "New Event", "new-event", 5, TicketTier.PaymentMethod.FREE
        )

        send_series_pass_extended(held_pass.id, [new_event.id])

        notifications = list(
            Notification.objects.filter(user=holder, notification_type=NotificationType.SERIES_PASS_EXTENDED)
        )
        assert len(notifications) == 1
        assert notifications[0].context["new_event_names"] == ["New Event"]
        assert notifications[0].context["new_event_count"] == 1


# --- Template rendering tests ---


def _make_purchased_notification(user: RevelUser) -> Notification:
    """Build a SERIES_PASS_PURCHASED notification with a valid context."""
    context = {
        "pass_id": "pass-id",
        "pass_name": "Season Pass",
        "series_id": "series-id",
        "series_name": "Weekly Classes",
        "organization_id": "org-id",
        "organization_name": "Acme Org",
        "event_count": 3,
        "price_paid": "20.00",
        "currency": "EUR",
    }
    return Notification.objects.create(
        user=user, notification_type=NotificationType.SERIES_PASS_PURCHASED, context=context
    )


def _make_extended_notification(user: RevelUser) -> Notification:
    """Build a SERIES_PASS_EXTENDED notification with a valid context."""
    context = {
        "pass_id": "pass-id",
        "pass_name": "Season Pass",
        "series_id": "series-id",
        "series_name": "Weekly Classes",
        "organization_id": "org-id",
        "organization_name": "Acme Org",
        "new_event_count": 2,
        "new_event_names": ["Extra Class 1", "Extra Class 2"],
    }
    return Notification.objects.create(
        user=user, notification_type=NotificationType.SERIES_PASS_EXTENDED, context=context
    )


class TestSeriesPassTemplatesRegistration:
    """Both types must resolve to their registered template instance."""

    def test_purchased_template_registered(self) -> None:
        assert is_template_registered(NotificationType.SERIES_PASS_PURCHASED)
        assert isinstance(get_template(NotificationType.SERIES_PASS_PURCHASED), SeriesPassPurchasedTemplate)

    def test_extended_template_registered(self) -> None:
        assert is_template_registered(NotificationType.SERIES_PASS_EXTENDED)
        assert isinstance(get_template(NotificationType.SERIES_PASS_EXTENDED), SeriesPassExtendedTemplate)


class TestSeriesPassPurchasedTemplateRendering:
    """Title/body/subject must surface the pass and series names."""

    def test_in_app_title_and_body(self, holder: RevelUser) -> None:
        notification = _make_purchased_notification(holder)
        template = SeriesPassPurchasedTemplate()

        title = template.get_in_app_title(notification)
        assert "Season Pass" in title
        assert "Weekly Classes" in title

        body = template.get_in_app_body(notification)
        assert "Season Pass" in body
        assert "Weekly Classes" in body

    def test_email_subject_and_body(self, holder: RevelUser) -> None:
        notification = _make_purchased_notification(holder)
        template = SeriesPassPurchasedTemplate()

        subject = template.get_email_subject(notification)
        assert "Season Pass" in subject

        text_body = template.get_email_text_body(notification)
        assert "Season Pass" in text_body
        assert "Weekly Classes" in text_body

        html_body = template.get_email_html_body(notification)
        assert html_body is not None
        assert "Season Pass" in html_body


class TestSeriesPassExtendedTemplateRendering:
    """Title/body/subject must surface the pass name and newly-covered events."""

    def test_in_app_title(self, holder: RevelUser) -> None:
        notification = _make_extended_notification(holder)
        template = SeriesPassExtendedTemplate()

        title = template.get_in_app_title(notification)
        assert "Season Pass" in title
        assert "2" in title

    def test_in_app_body_lists_events(self, holder: RevelUser) -> None:
        notification = _make_extended_notification(holder)
        template = SeriesPassExtendedTemplate()

        body = template.get_in_app_body(notification)
        assert "Season Pass" in body
        assert "Weekly Classes" in body
        assert "Extra Class 1" in body
        assert "Extra Class 2" in body

    def test_email_subject_and_body(self, holder: RevelUser) -> None:
        notification = _make_extended_notification(holder)
        template = SeriesPassExtendedTemplate()

        subject = template.get_email_subject(notification)
        assert "Season Pass" in subject

        text_body = template.get_email_text_body(notification)
        assert "Season Pass" in text_body
        assert "Extra Class 1" in text_body

        html_body = template.get_email_html_body(notification)
        assert html_body is not None
        assert "Extra Class 1" in html_body
