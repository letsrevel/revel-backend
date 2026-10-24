"""Attendee-facing notification contexts build their event link on the org's custom domain (#1148 review)."""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization, Payment, Ticket, TicketTier
from notifications.signals import event as event_signals
from notifications.signals import payment as payment_signals

pytestmark = pytest.mark.django_db

HOST = "events.example.com"
PLATFORM = "https://app.letsrevel.io"


@pytest.fixture(autouse=True)
def platform_url() -> None:
    site = SiteSettings.get_solo()
    site.frontend_base_url = PLATFORM
    site.save()


def _make_tenant(org: Organization, *, enabled: bool = True) -> None:
    Organization.objects.filter(pk=org.pk).update(custom_domain=HOST, custom_domain_enabled=enabled)


def _event_url(public_event: Event, send_mock: MagicMock) -> str:
    return str(send_mock.call_args.kwargs["context"]["event_url"])


class TestPaymentConfirmationLink:
    @pytest.fixture
    def payment(self, public_event: Event, member_user: RevelUser) -> Payment:
        tier = TicketTier.objects.create(
            event=public_event, name="Paid", price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.ONLINE
        )
        ticket = Ticket.objects.create(
            guest_name="G", event=public_event, user=member_user, tier=tier, status=Ticket.TicketStatus.ACTIVE
        )
        return Payment.objects.create(
            ticket=ticket,
            user=member_user,
            amount=Decimal("10.00"),
            platform_fee=Decimal("0.00"),
            currency="EUR",
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_session_id="cs_test_tenant",
        )

    def test_tenant_org_links_to_its_domain(self, payment: Payment, public_event: Event) -> None:
        _make_tenant(public_event.organization)
        payment = Payment.objects.select_related("ticket__event__organization").get(pk=payment.pk)

        with patch("notifications.signals.payment.notification_requested.send") as send_mock:
            payment_signals._send_payment_confirmation(payment)

        assert _event_url(public_event, send_mock) == f"https://{HOST}/events/{public_event.id}"

    def test_non_tenant_org_links_to_the_platform(self, payment: Payment, public_event: Event) -> None:
        with patch("notifications.signals.payment.notification_requested.send") as send_mock:
            payment_signals._send_payment_confirmation(payment)

        assert _event_url(public_event, send_mock) == f"{PLATFORM}/events/{public_event.id}"


class TestEventNotificationLinks:
    @staticmethod
    def _eligible(recipient: RevelUser) -> MagicMock:
        eligible = MagicMock()
        eligible.__iter__.return_value = iter([recipient])
        eligible.count.return_value = 1
        return eligible

    @pytest.mark.parametrize("kind", ["cancelled", "updated"])
    def test_tenant_org_links_to_its_domain(
        self,
        kind: str,
        public_event: Event,
        user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _make_tenant(public_event.organization)
        public_event.organization.refresh_from_db()

        with (
            patch.object(event_signals, "get_eligible_users_for_event_notification", return_value=self._eligible(user)),
            patch.object(event_signals, "get_event_location_for_user", return_value=("Somewhere", None)),
            patch("notifications.signals.event.notification_requested.send") as send_mock,
            django_capture_on_commit_callbacks(execute=True),
        ):
            if kind == "cancelled":
                event_signals._handle_event_cancelled(Event, public_event)
            else:
                event_signals._handle_event_updated(Event, public_event, ["name"], {"name": "a"}, {"name": "b"})

        assert _event_url(public_event, send_mock) == f"https://{HOST}/events/{public_event.id}"
