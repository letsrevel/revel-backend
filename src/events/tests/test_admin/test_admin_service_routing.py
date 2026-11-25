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
from events.service import discount_code_service, event_update_service

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
        with (
            patch(EXPIRE) as expire,
            patch(
                "events.admin.ticket.discount_code_service.release_uses_of_pending_tickets",
                wraps=discount_code_service.release_uses_of_pending_tickets,
            ) as release,
            django_capture_on_commit_callbacks(execute=True),
        ):
            if via == "delete_view":
                _delete_view(admin_client, "organization", connected_org.pk)
            else:
                _bulk(admin_client, "organization", "delete_selected", [connected_org.pk], post="yes")

        assert not Organization.objects.filter(pk=connected_org.pk).exists()
        expire.assert_called_once_with("cs_org", stripe_account="acct_connected")
        # The code cascades away with the organization, so assert on the release call itself.
        release.assert_called_once()


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

    def test_change_view_with_malformed_id_is_not_a_500(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:events_payment_change", args=["not-a-uuid"]))

        assert response.status_code == 302

    def test_bulk_delete_of_pending_payment_shows_protected_page(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment

        response = _bulk(admin_client, "payment", "delete_selected", [pending.pk], post="yes")

        assert Payment.objects.filter(pk=pending.pk).exists()
        assert b"pending payment" in response.content

    def test_bulk_delete_with_a_pending_payment_deletes_nothing(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment
        done = _checkout(tier, member_user, "cs_d", status=Payment.PaymentStatus.SUCCEEDED).payment

        _bulk(admin_client, "payment", "delete_selected", [pending.pk, done.pk], post="yes")

        assert Payment.objects.filter(pk__in=[pending.pk, done.pk]).count() == 2

    def test_bulk_delete_of_settled_payment_works(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        done = _checkout(tier, member_user, "cs_d", status=Payment.PaymentStatus.SUCCEEDED).payment

        _bulk(admin_client, "payment", "delete_selected", [done.pk], post="yes")

        assert not Payment.objects.filter(pk=done.pk).exists()

    def test_delete_view_of_pending_payment_is_refused(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        pending = _checkout(tier, member_user, "cs_p").payment

        response = _delete_view(admin_client, "payment", pending.pk)

        assert Payment.objects.filter(pk=pending.pk).exists()
        assert b"pending payment" in response.content

    def test_pending_checkout_does_not_block_deleting_its_ticket(
        self, admin_client: Client, tier: TicketTier, member_user: RevelUser
    ) -> None:
        """The guard must not live in ``has_delete_permission``: cascades would 403."""
        ticket = _checkout(tier, member_user, "cs_p")

        with patch(EXPIRE):
            response = _delete_view(admin_client, "ticket", ticket.pk)

        assert response.status_code == 200
        assert not Ticket.objects.filter(pk=ticket.pk).exists()


class TestInlinesCannotDelete:
    def test_tier_and_series_inlines_have_no_delete(self, rf: t.Any, superuser: RevelUser, event: Event) -> None:
        from django.contrib.admin.sites import site

        request = rf.get("/")
        request.user = superuser
        seen = set()
        for model, parent in ((Event, event), (Organization, event.organization)):
            model_admin = site._registry[model]
            for inline in model_admin.get_inline_instances(request, parent):
                if inline.model in (TicketTier, EventSeries):
                    seen.add(inline.model)
                    assert inline.get_formset(request, parent)().can_delete is False
        assert seen == {TicketTier, EventSeries}  # both inlines are still on their parent pages


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

    def test_actions_need_change_permission(self, rf: t.Any, member_user: RevelUser) -> None:
        from django.contrib.admin.sites import site
        from django.contrib.auth.models import Permission

        member_user.is_staff = True
        member_user.user_permissions.add(Permission.objects.get(codename="view_event"))
        request = rf.get("/")
        request.user = RevelUser.objects.get(pk=member_user.pk)
        model_admin = site._registry[Event]

        assert not {"open_events", "close_events", "draft_events", "cancel_events"} & set(
            model_admin.get_actions(request)
        )

        member_user.user_permissions.add(Permission.objects.get(codename="change_event"))
        request.user = RevelUser.objects.get(pk=member_user.pk)

        assert {"open_events", "close_events", "draft_events", "cancel_events"} <= set(model_admin.get_actions(request))

    def test_cancel_keeps_the_typed_reason(self, admin_client: Client, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(cancellation_reason="Venue flooded")

        _bulk(admin_client, "event", "cancel_events", [event.pk])

        event.refresh_from_db()
        assert event.status == Event.EventStatus.CANCELLED
        assert event.cancellation_reason == "Venue flooded"

    def test_cancel_of_cancelled_event_is_skipped(self, admin_client: Client, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.CANCELLED, cancellation_reason="Flood")

        with patch("events.admin.event.event_update_service.update_status") as update_status:
            response = _bulk(admin_client, "event", "cancel_events", [event.pk])

        update_status.assert_not_called()
        event.refresh_from_db()
        assert event.cancellation_reason == "Flood"
        assert not any("Set 0" in m for m in _messages(response))
        assert any("No event changed" in m for m in _messages(response))

    def test_cancel_decides_on_the_current_row_not_the_changelist_snapshot(
        self, admin_client: Client, event: Event, organization: Organization
    ) -> None:
        """An event cancelled elsewhere while the action runs keeps that cancellation and its reason."""
        other = Event.objects.create(
            organization=organization,
            name="Other",
            slug="other-race",
            start=event.start,
            end=event.end,
            status=Event.EventStatus.OPEN,
            requires_ticket=False,
        )
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.OPEN)
        real_update_status = event_update_service.update_status

        cancelled_elsewhere: list[t.Any] = []

        def cancel_the_other_meanwhile(target: Event, *args: t.Any, **kwargs: t.Any) -> Event:
            result = real_update_status(target, *args, **kwargs)
            if not cancelled_elsewhere:  # whichever runs first: an organizer cancels the other one meanwhile
                pk = other.pk if target.pk == event.pk else event.pk
                Event.objects.filter(pk=pk).update(
                    status=Event.EventStatus.CANCELLED, cancellation_reason="Venue flooded"
                )
                cancelled_elsewhere.append(pk)
            return result

        with patch("events.admin.event.event_update_service.update_status", side_effect=cancel_the_other_meanwhile):
            _bulk(admin_client, "event", "cancel_events", [event.pk, other.pk])

        row = Event.objects.get(pk=cancelled_elsewhere[0])
        assert (row.status, row.cancellation_reason) == (Event.EventStatus.CANCELLED, "Venue flooded")

    def test_invalid_event_is_reported_and_others_still_change(
        self, admin_client: Client, event: Event, organization: Organization
    ) -> None:
        other = Event.objects.create(
            organization=organization,
            name="Other",
            slug="other-ev",
            start=event.start,
            end=event.end,
            status=Event.EventStatus.DRAFT,
            requires_ticket=False,
        )
        Event.objects.filter(pk=event.pk).update(
            status=Event.EventStatus.DRAFT, waitlist_cutoff_date=event.start + timedelta(days=1)
        )
        Event.objects.filter(pk=other.pk).update(status=Event.EventStatus.DRAFT)

        response = _bulk(admin_client, "event", "open_events", [event.pk, other.pk])

        assert response.status_code == 200
        event.refresh_from_db()
        other.refresh_from_db()
        assert event.status == Event.EventStatus.DRAFT
        assert other.status == Event.EventStatus.OPEN
        assert any(event.name in m for m in _messages(response))
