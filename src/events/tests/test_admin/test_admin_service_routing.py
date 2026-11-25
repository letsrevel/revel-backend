"""Django admin deletes and status changes go through the services (#1499, #1535)."""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db.models import F
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, HeldSeriesPass, Organization, Payment, SeriesPass, Ticket, TicketTier
from events.models.discount_code import DiscountCode

pytestmark = pytest.mark.django_db

EXPIRE = "events.service.pending_checkout.Session.expire"


@pytest.fixture(autouse=True)
def plain_static_storage(settings: t.Any) -> None:
    """Admin pages render static URLs; the hashed manifest storage has no manifest in tests."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(superuser: RevelUser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def connected_org(organization: Organization) -> Organization:
    Organization.objects.filter(pk=organization.pk).update(stripe_account_id="acct_connected")
    organization.refresh_from_db()
    return organization


@pytest.fixture
def code(organization: Organization) -> DiscountCode:
    return DiscountCode.objects.create(
        code="ADMCODE",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        max_uses=10,
        is_active=True,
    )


@pytest.fixture
def tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="Online", price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.ONLINE
    )


def _checkout(
    tier: TicketTier,
    user: RevelUser,
    session_id: str,
    *,
    status: str = Payment.PaymentStatus.PENDING,
    code: DiscountCode | None = None,
) -> Ticket:
    pending = status == Payment.PaymentStatus.PENDING
    ticket = Ticket.objects.create(
        event=tier.event,
        tier=tier,
        user=user,
        guest_name="Holder",
        status=Ticket.TicketStatus.PENDING if pending else Ticket.TicketStatus.ACTIVE,
        discount_code=code,
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        amount=Decimal("9.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=status,
        reservation_id=uuid.uuid4(),
        stripe_session_id=session_id,
        expires_at=timezone.now() + timedelta(hours=1),
        raw_response={},
    )
    if code is not None:
        DiscountCode.objects.filter(pk=code.pk).update(times_used=F("times_used") + 1)
    return ticket


def _used(code: DiscountCode) -> int:
    code.refresh_from_db()
    return code.times_used


def _delete_view(admin_client: Client, model: str, pk: t.Any) -> t.Any:
    return admin_client.post(reverse(f"admin:events_{model}_delete", args=[pk]), {"post": "yes"}, follow=True)


def _bulk(admin_client: Client, model: str, action: str, pks: list[t.Any], **extra: t.Any) -> t.Any:
    data = {"action": action, "_selected_action": [str(pk) for pk in pks], **extra}
    return admin_client.post(reverse(f"admin:events_{model}_changelist"), data, follow=True)


def _messages(response: t.Any) -> list[str]:
    return [str(m) for m in response.context["messages"]]


class TestAdminDeletes:
    @pytest.mark.parametrize("via", ["delete_view", "delete_selected"])
    def test_event_delete(
        self,
        admin_client: Client,
        connected_org: Organization,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        code: DiscountCode,
        via: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _checkout(tier, member_user, "cs_event", code=code)
        with patch(EXPIRE) as expire, django_capture_on_commit_callbacks(execute=True):
            if via == "delete_view":
                _delete_view(admin_client, "event", event.pk)
            else:
                _bulk(admin_client, "event", "delete_selected", [event.pk], post="yes")

        assert not Event.objects.filter(pk=event.pk).exists()
        expire.assert_called_once_with("cs_event", stripe_account="acct_connected")
        assert _used(code) == 0

    @pytest.mark.parametrize("via", ["delete_view", "delete_selected"])
    def test_ticket_tier_delete(
        self,
        admin_client: Client,
        connected_org: Organization,
        tier: TicketTier,
        member_user: RevelUser,
        code: DiscountCode,
        via: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _checkout(tier, member_user, "cs_tier", code=code)
        with patch(EXPIRE) as expire, django_capture_on_commit_callbacks(execute=True):
            if via == "delete_view":
                _delete_view(admin_client, "tickettier", tier.pk)
            else:
                _bulk(admin_client, "tickettier", "delete_selected", [tier.pk], post="yes")

        assert not TicketTier.objects.filter(pk=tier.pk).exists()
        expire.assert_called_once_with("cs_tier", stripe_account="acct_connected")
        assert _used(code) == 0

    @pytest.mark.parametrize("via", ["delete_view", "delete_selected"])
    def test_event_series_delete(
        self,
        admin_client: Client,
        connected_org: Organization,
        event_series: EventSeries,
        tier: TicketTier,
        member_user: RevelUser,
        code: DiscountCode,
        via: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _checkout(tier, member_user, "cs_series", code=code)
        with patch(EXPIRE) as expire, django_capture_on_commit_callbacks(execute=True):
            if via == "delete_view":
                _delete_view(admin_client, "eventseries", event_series.pk)
            else:
                _bulk(admin_client, "eventseries", "delete_selected", [event_series.pk], post="yes")

        assert not EventSeries.objects.filter(pk=event_series.pk).exists()
        expire.assert_called_once_with("cs_series", stripe_account="acct_connected")
        assert _used(code) == 0

    def test_event_series_with_holders_is_refused_with_a_message(
        self,
        rf: t.Any,
        superuser: RevelUser,
        event_series: EventSeries,
        member_user: RevelUser,
    ) -> None:
        """The admin's own protected-objects page normally stops this first; the service error is the backstop."""
        from django.contrib.admin.sites import site
        from django.contrib.messages.storage.fallback import FallbackStorage

        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Season",
            price=Decimal("30.00"),
            pro_rata_discount=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED,
            price_paid=series_pass.price,
        )
        request = rf.post("/")
        request.user = superuser
        request.session = {}
        request._messages = FallbackStorage(request)

        site._registry[EventSeries].delete_model(request, event_series)

        assert EventSeries.objects.filter(pk=event_series.pk).exists()
        assert any("sold series passes" in str(m) for m in request._messages)

    @pytest.mark.parametrize("via", ["delete_view", "delete_selected"])
    def test_ticket_delete(
        self,
        admin_client: Client,
        connected_org: Organization,
        tier: TicketTier,
        member_user: RevelUser,
        code: DiscountCode,
        via: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        ticket = _checkout(tier, member_user, "cs_ticket", code=code)
        with patch(EXPIRE) as expire, django_capture_on_commit_callbacks(execute=True):
            if via == "delete_view":
                _delete_view(admin_client, "ticket", ticket.pk)
            else:
                _bulk(admin_client, "ticket", "delete_selected", [ticket.pk], post="yes")

        assert not Ticket.objects.filter(pk=ticket.pk).exists()
        expire.assert_called_once_with("cs_ticket", stripe_account="acct_connected")
        assert _used(code) == 0

    @pytest.mark.parametrize("via", ["delete_view", "delete_selected"])
    def test_organization_delete(
        self,
        admin_client: Client,
        connected_org: Organization,
        tier: TicketTier,
        member_user: RevelUser,
        code: DiscountCode,
        via: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _checkout(tier, member_user, "cs_org", code=code)
        with patch(EXPIRE) as expire, django_capture_on_commit_callbacks(execute=True):
            if via == "delete_view":
                _delete_view(admin_client, "organization", connected_org.pk)
            else:
                _bulk(admin_client, "organization", "delete_selected", [connected_org.pk], post="yes")

        assert not Organization.objects.filter(pk=connected_org.pk).exists()
        expire.assert_called_once_with("cs_org", stripe_account="acct_connected")
        # The code cascades away with the organization; the point is that the release ran without error.


class TestPendingPaymentDelete:
    def test_change_page_hides_delete_for_pending_only(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment
        done = _checkout(tier, member_user, "cs_d", status=Payment.PaymentStatus.SUCCEEDED).payment

        pending_page = admin_client.get(reverse("admin:events_payment_change", args=[pending.pk]))
        done_page = admin_client.get(reverse("admin:events_payment_change", args=[done.pk]))

        assert pending_page.context["show_delete"] is False
        assert done_page.context["show_delete"] is True

    def test_bulk_delete_keeps_pending_and_warns(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment
        done = _checkout(tier, member_user, "cs_d", status=Payment.PaymentStatus.SUCCEEDED).payment

        response = _bulk(admin_client, "payment", "delete_selected", [pending.pk, done.pk], post="yes")

        assert Payment.objects.filter(pk=pending.pk).exists()
        assert not Payment.objects.filter(pk=done.pk).exists()
        assert any("pending" in m.lower() for m in _messages(response))

    def test_delete_view_of_pending_payment_is_refused(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment

        response = _delete_view(admin_client, "payment", pending.pk)

        assert Payment.objects.filter(pk=pending.pk).exists()
        assert any("pending payment" in m.lower() for m in _messages(response))


class TestAdminStatus:
    def test_status_is_read_only_on_existing_events_only(self, rf: t.Any, superuser: RevelUser, event: Event) -> None:
        from django.contrib.admin.sites import site

        request = rf.get("/")
        request.user = superuser
        model_admin = site._registry[Event]

        assert "status" in model_admin.get_readonly_fields(request, event)
        assert "status" not in model_admin.get_readonly_fields(request, None)

    @pytest.mark.parametrize(
        ("action", "target"),
        [
            ("open_events", Event.EventStatus.OPEN),
            ("close_events", Event.EventStatus.CLOSED),
            ("draft_events", Event.EventStatus.DRAFT),
            ("cancel_events", Event.EventStatus.CANCELLED),
        ],
    )
    def test_actions_call_update_status(
        self, admin_client: Client, event: Event, superuser: RevelUser, action: str, target: Event.EventStatus
    ) -> None:
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.DRAFT if target != "draft" else "open")

        with patch("events.admin.event.event_update_service.update_status") as update_status:
            _bulk(admin_client, "event", action, [event.pk])

        update_status.assert_called_once()
        assert update_status.call_args.args[1] == target
        assert update_status.call_args.kwargs["initiated_by"] == superuser

    def test_cancel_revokes_offers_and_uncancel_after_refunds_is_refused(
        self, admin_client: Client, event: Event
    ) -> None:
        with patch("events.service.event_update_service.revoke_all_pending_offers") as revoke:
            _bulk(admin_client, "event", "cancel_events", [event.pk])
        revoke.assert_called_once_with(event.id)
        event.refresh_from_db()
        assert event.status == Event.EventStatus.CANCELLED

        Event.objects.filter(pk=event.pk).update(tickets_refund_started_at=timezone.now())
        response = _bulk(admin_client, "event", "open_events", [event.pk])

        event.refresh_from_db()
        assert event.status == Event.EventStatus.CANCELLED
        assert any(event.name in m for m in _messages(response))
