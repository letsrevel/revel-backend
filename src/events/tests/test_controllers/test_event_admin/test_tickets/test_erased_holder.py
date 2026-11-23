"""Staff see an erased holder with the kept name and email only, and can still check them in (#1476)."""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from conftest import RevelUserFactory
from events.models import Event, Ticket, TicketTier
from events.schema.staff_view_user import staff_view_user
from events.service.export.attendee_export import _write_attendees_sheet
from events.service.holder_visibility import annotate_holder_known
from events.utils import create_ticket_pdf

pytestmark = pytest.mark.django_db

HOLDER_EMAIL = "kept-holder@example.com"


@pytest.fixture
def erased_ticket(revel_user_factory: RevelUserFactory, event: Event, event_ticket_tier: TicketTier) -> Ticket:
    """An ACTIVE ticket on the open-for-check-in event whose holder deleted the account."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        check_in_starts_at=now - timedelta(hours=1), check_in_ends_at=now + timedelta(hours=1)
    )
    holder = revel_user_factory.create_user(
        username=HOLDER_EMAIL, email=HOLDER_EMAIL, first_name="Grace", last_name="Hopper", preferred_name=""
    )
    ticket = Ticket.objects.create(event=event, tier=event_ticket_tier, user=holder, status=Ticket.TicketStatus.ACTIVE)
    delete_user_account(str(holder.pk))
    return Ticket.objects.select_related("user", "event", "tier").get(pk=ticket.pk)


def _ticket_row(client: Client, ticket: Ticket) -> dict[str, t.Any]:
    response = client.get(reverse("api:list_tickets", kwargs={"event_id": ticket.event_id}))
    assert response.status_code == 200, response.content
    return next(row for row in response.json()["results"] if row["id"] == str(ticket.pk))


def test_staff_see_the_kept_name_and_email(organization_owner_client: Client, erased_ticket: Ticket) -> None:
    user = _ticket_row(organization_owner_client, erased_ticket)["user"]

    assert user["erased"] is True
    assert user["display_name"] == "Grace Hopper"
    assert user["email"] == HOLDER_EMAIL
    assert user["first_name"] is None and user["bio"] is None


def test_without_a_kept_name_the_holder_shows_as_the_kept_email(erased_ticket: Ticket) -> None:
    view = staff_view_user(erased_ticket.user, known=True, supplied_name="", holder_email=HOLDER_EMAIL)

    assert (view.erased, view.display_name, view.email) == (True, HOLDER_EMAIL, HOLDER_EMAIL)


def test_without_a_kept_name_and_email_the_holder_is_a_deleted_user(erased_ticket: Ticket) -> None:
    view = staff_view_user(erased_ticket.user, known=True, supplied_name="", holder_email="")

    assert (view.erased, view.display_name, view.email) == (True, "Deleted user", None)


def test_a_holder_that_the_organization_did_not_know_keeps_no_profile_name(
    organization_owner_client: Client, revel_user_factory: RevelUserFactory, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Staff saw only the email of this holder before the erasure (a guest checkout that never confirmed its email)."""
    email = "walk-in@example.com"
    holder = revel_user_factory.create_user(username=email, email=email, first_name="Grace", last_name="Hopper")
    ticket = Ticket.objects.create(
        event=event,
        tier=event_ticket_tier,
        user=holder,
        status=Ticket.TicketStatus.ACTIVE,
        unconfirmed_guest=True,
    )
    before = _ticket_row(organization_owner_client, ticket)["user"]
    assert (before["known"], before["display_name"]) == (False, email)

    delete_user_account(str(holder.pk))

    ticket.refresh_from_db()
    assert (ticket.guest_name, ticket.holder_email) == ("", email)
    after = _ticket_row(organization_owner_client, ticket)["user"]
    assert (after["erased"], after["display_name"], after["email"]) == (True, email, email)
    assert "Grace" not in str(after) and "Hopper" not in str(after)


def test_a_live_account_is_not_marked_erased(member_user: RevelUser) -> None:
    assert staff_view_user(member_user, known=True).erased is False


def test_check_in_of_an_erased_holder_works(organization_owner_client: Client, erased_ticket: Ticket) -> None:
    url = reverse("api:check_in_ticket", kwargs={"event_id": erased_ticket.event_id, "code": erased_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    assert response.json()["user"]["erased"] is True
    erased_ticket.refresh_from_db()
    assert erased_ticket.status == Ticket.TicketStatus.CHECKED_IN


@pytest.mark.parametrize("client_fixture", ["member_client", "nonmember_client"])
def test_people_who_are_not_staff_are_refused_the_ticket_list_with_the_kept_email(
    request: pytest.FixtureRequest, client_fixture: str, erased_ticket: Ticket
) -> None:
    """The staff ticket list is the surface that carries the snapshot; only staff may read it.

    The member-facing attendee list reads account fields only (``AttendeeSchema``), never the ticket snapshot.
    """
    client: Client = request.getfixturevalue(client_fixture)

    response = client.get(reverse("api:list_tickets", kwargs={"event_id": erased_ticket.event_id}))

    assert response.status_code in (403, 404)
    assert HOLDER_EMAIL not in response.content.decode()


def test_staff_find_an_erased_holder_by_the_kept_email(
    organization_owner_client: Client, erased_ticket: Ticket
) -> None:
    response = organization_owner_client.get(
        reverse("api:list_tickets", kwargs={"event_id": erased_ticket.event_id}), {"search": HOLDER_EMAIL}
    )

    assert [row["id"] for row in response.json()["results"]] == [str(erased_ticket.pk)]


def test_a_kept_email_that_django_would_reject_does_not_block_check_in(
    organization_owner_client: Client, erased_ticket: Ticket
) -> None:
    """The snapshot is stored as typed at signup; a later save (check-in) must never re-validate it."""
    Ticket.objects.filter(pk=erased_ticket.pk).update(holder_email="jörg@example.com")
    url = reverse("api:check_in_ticket", kwargs={"event_id": erased_ticket.event_id, "code": erased_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content


def test_staff_refund_and_unmatched_refund_alerts_name_the_kept_email(erased_ticket: Ticket) -> None:
    from decimal import Decimal

    from events.models import Payment
    from notifications.signals import payment as payment_signals

    payment = Payment.objects.create(
        ticket=erased_ticket,
        user=erased_ticket.user,
        amount=Decimal("10.00"),
        platform_fee=Decimal("0.50"),
        currency="EUR",
        status=Payment.PaymentStatus.REFUNDED,
        stripe_session_id="cs_erased",
        stripe_payment_intent_id="pi_erased",
    )
    with patch.object(payment_signals, "notify_org_staff") as notify:
        payment_signals._send_refund_notifications(payment)
        payment_signals.send_refund_unmatched(
            payment_intent_id="pi_erased",
            refund_id="re_1",
            refund_amount=Decimal("1.00"),
            currency="EUR",
            reason="",
            candidates=[payment],
        )

    contexts = [c.kwargs["context"] for c in notify.call_args_list]
    assert contexts[0]["ticket_holder_email"] == HOLDER_EMAIL
    assert [candidate["holder_email"] for candidate in contexts[1]["candidates"]] == [HOLDER_EMAIL]


def test_the_attendee_export_has_the_kept_name_and_email(erased_ticket: Ticket) -> None:
    from openpyxl import Workbook

    tickets = list(
        annotate_holder_known(
            Ticket.objects.filter(pk=erased_ticket.pk), erased_ticket.event.organization_id
        ).select_related("user", "tier", "seat")
    )
    workbook = Workbook()

    _write_attendees_sheet(workbook, tickets, [])

    rows = list(workbook["Attendees"].iter_rows(values_only=True))
    assert rows[1][:2] == ("Grace Hopper", HOLDER_EMAIL)


def test_the_ticket_pdf_shows_the_kept_name(erased_ticket: Ticket) -> None:
    with patch("events.utils.render_to_string", return_value="<p></p>") as render, patch("weasyprint.HTML"):
        create_ticket_pdf(erased_ticket)

    assert render.call_args.kwargs["context"]["user_display_name"] == "Grace Hopper"


class TestAHolderWhoKeptNoName:
    """An account without names keeps no name: every holder surface still shows something."""

    @pytest.fixture
    def nameless(self, erased_ticket: Ticket) -> Ticket:
        Ticket.objects.filter(pk=erased_ticket.pk).update(guest_name="")
        return Ticket.objects.select_related("user", "event", "tier").get(pk=erased_ticket.pk)

    def test_the_ticket_pdf_shows_deleted_user(self, nameless: Ticket) -> None:
        with patch("events.utils.render_to_string", return_value="<p></p>") as render, patch("weasyprint.HTML"):
            create_ticket_pdf(nameless)

        assert render.call_args.kwargs["context"]["user_display_name"] == "Deleted user"

    def test_a_day_membership_record_gets_the_kept_email_as_its_name(self, nameless: Ticket) -> None:
        from events.models import MembershipTier
        from events.service.day_membership_service import _record_kwargs

        tier = MembershipTier.objects.create(organization=nameless.event.organization, name="Day")

        kwargs = _record_kwargs(nameless, tier, nameless.event, None)

        assert (kwargs["full_name"], kwargs["email"]) == (HOLDER_EMAIL, HOLDER_EMAIL)
