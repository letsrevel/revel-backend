"""Staff alerts are sent as their own notification types, never as the buyer's (#1127)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import transaction

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Payment, Ticket, TicketSaleSource, TicketTier
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db(transaction=True)


def _tier(event: Event, payment_method: TicketTier.PaymentMethod, price: str = "0.00") -> TicketTier:
    return TicketTier.objects.create(
        event=event, name="Alert Tier", price=Decimal(price), currency="EUR", payment_method=payment_method
    )


def test_ticket_created_sends_buyer_type_to_holder_and_staff_type_to_owner(
    public_event: Event, organization: Organization, member_user: RevelUser
) -> None:
    tier = _tier(public_event, TicketTier.PaymentMethod.FREE)

    with patch("notifications.signals.ticket.notification_requested.send") as send_mock:
        with transaction.atomic():
            Ticket.objects.create(
                event=public_event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=member_user.get_display_name(),
            )

    by_type = {(c.kwargs["notification_type"], c.kwargs["user"]): c.kwargs["context"] for c in send_mock.call_args_list}
    assert (NotificationType.TICKET_CREATED, member_user) in by_type
    assert (NotificationType.TICKET_CREATED, organization.owner) not in by_type
    staff_context = by_type[(NotificationType.STAFF_TICKET_CREATED, organization.owner)]
    assert staff_context["ticket_holder_name"] == member_user.get_display_name()
    assert staff_context["ticket_holder_email"] == member_user.email
    # Staff templates attach nothing, so the attachment switches are not sent.
    assert not {"include_pdf", "include_ics", "include_pkpass", "staff_alert"} & staff_context.keys()
    holder_context = by_type[(NotificationType.TICKET_CREATED, member_user)]
    assert "ticket_holder_name" not in holder_context


def test_ticket_cancelled_sends_staff_type_to_owner(
    public_event: Event, organization: Organization, member_user: RevelUser
) -> None:
    tier = _tier(public_event, TicketTier.PaymentMethod.FREE)
    ticket = Ticket.objects.create(
        event=public_event,
        tier=tier,
        user=member_user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )

    with patch("notifications.signals.ticket.notification_requested.send") as send_mock:
        with transaction.atomic():
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save()

    sent = {(c.kwargs["notification_type"], c.kwargs["user"]) for c in send_mock.call_args_list}
    assert (NotificationType.TICKET_CANCELLED, member_user) in sent
    assert (NotificationType.STAFF_TICKET_CANCELLED, organization.owner) in sent
    assert (NotificationType.TICKET_CANCELLED, organization.owner) not in sent


def test_ticket_refunded_sends_staff_type_to_owner(
    public_event: Event, organization: Organization, member_user: RevelUser
) -> None:
    tier = _tier(public_event, TicketTier.PaymentMethod.ONLINE, "40.00")
    ticket = Ticket.objects.create(
        event=public_event,
        tier=tier,
        user=member_user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=member_user,
        amount=Decimal("40.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=Payment.PaymentStatus.SUCCEEDED,
        stripe_session_id=f"cs_test_{ticket.id}",
    )

    with patch("notifications.signals.payment.notification_requested.send") as send_mock:
        with transaction.atomic():
            payment.status = Payment.PaymentStatus.REFUNDED
            payment.save()

    sent: dict[tuple[str, RevelUser], dict[str, t.Any]] = {
        (c.kwargs["notification_type"], c.kwargs["user"]): c.kwargs["context"] for c in send_mock.call_args_list
    }
    assert (NotificationType.TICKET_REFUNDED, member_user) in sent
    assert (NotificationType.TICKET_REFUNDED, organization.owner) not in sent
    assert (
        sent[(NotificationType.STAFF_TICKET_REFUNDED, organization.owner)]["ticket_holder_email"] == member_user.email
    )


def _staff_contexts(send_mock: t.Any, notification_type: str) -> list[dict[str, t.Any]]:
    return [c.kwargs["context"] for c in send_mock.call_args_list if c.kwargs["notification_type"] == notification_type]


def test_staff_context_for_box_office_holder_carries_typed_name_not_account_name(
    public_event: Event, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """A holder known only through a box-office ticket shows staff the typed name, never the account name."""
    stranger = revel_user_factory(username="stranger", first_name="Legal", last_name="Surname")
    tier = _tier(public_event, TicketTier.PaymentMethod.FREE)

    with patch("notifications.signals.ticket.notification_requested.send") as send_mock:
        with transaction.atomic():
            ticket = Ticket.objects.create(
                event=public_event,
                tier=tier,
                user=stranger,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name="Typed Name",
                sale_source=TicketSaleSource.BOX_OFFICE_COMP,
            )
    (staff_context,) = _staff_contexts(send_mock, NotificationType.STAFF_TICKET_CREATED)
    assert staff_context["ticket_holder_name"] == "Typed Name"
    assert staff_context["ticket_holder_email"] == stranger.email
    assert "Legal" not in str(staff_context)

    with patch("notifications.signals.ticket.notification_requested.send") as send_mock:
        with transaction.atomic():
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save()
    (cancel_context,) = _staff_contexts(send_mock, NotificationType.STAFF_TICKET_CANCELLED)
    assert cancel_context["ticket_holder_name"] == "Typed Name"


def test_staff_context_for_unknown_holder_falls_back_to_email(
    public_event: Event, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    stranger = revel_user_factory(username="stranger2", first_name="Legal", last_name="Surname")
    tier = _tier(public_event, TicketTier.PaymentMethod.FREE)
    with patch("notifications.signals.ticket.notification_requested.send") as send_mock:
        with transaction.atomic():
            Ticket.objects.create(
                event=public_event,
                tier=tier,
                user=stranger,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name="",
                sale_source=TicketSaleSource.BOX_OFFICE_SALE,
            )
    (staff_context,) = _staff_contexts(send_mock, NotificationType.STAFF_TICKET_CREATED)
    assert staff_context["ticket_holder_name"] == stranger.email


def test_staff_refund_context_for_box_office_holder_carries_typed_name(
    public_event: Event, organization: Organization, revel_user_factory: RevelUserFactory
) -> None:
    stranger = revel_user_factory(username="stranger3", first_name="Legal", last_name="Surname")
    tier = _tier(public_event, TicketTier.PaymentMethod.ONLINE, "40.00")
    ticket = Ticket.objects.create(
        event=public_event,
        tier=tier,
        user=stranger,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Typed Name",
        sale_source=TicketSaleSource.BOX_OFFICE_SALE,
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=stranger,
        amount=Decimal("40.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=Payment.PaymentStatus.SUCCEEDED,
        stripe_session_id=f"cs_test_{ticket.id}",
    )
    with patch("notifications.signals.payment.notification_requested.send") as send_mock:
        with transaction.atomic():
            payment.status = Payment.PaymentStatus.REFUNDED
            payment.save()
    (staff_context,) = _staff_contexts(send_mock, NotificationType.STAFF_TICKET_REFUNDED)
    assert staff_context["ticket_holder_name"] == "Typed Name"
