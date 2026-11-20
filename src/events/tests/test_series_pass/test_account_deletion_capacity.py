"""Deleting an account gives back the slots of its unfinished checkouts only; issued tickets keep theirs (#1447)."""

from uuid import UUID

import pytest

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _ticket(user: RevelUser, event: Event, tier: TicketTier, status: str) -> Ticket:
    return Ticket.objects.create(event=event, tier=tier, user=user, status=status, guest_name="Holder")


def _sold(model: type[TicketTier] | type[SeriesPass], pk: UUID) -> int:
    return model.objects.get(pk=pk).quantity_sold


class TestAccountDeletionCapacity:
    def test_pending_tickets_give_back_their_slots(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=5)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 3

    @pytest.mark.parametrize(
        "status", [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED]
    )
    def test_issued_and_cancelled_tickets_keep_the_count(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier, status: str
    ) -> None:
        """An issued ticket keeps its slot, so its place cannot be sold a second time."""
        _ticket(revel_user, event, ticket_tier, status)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=5)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 5

    def test_the_decrement_is_floored_at_zero(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=1)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 0

    def test_a_pending_held_pass_gives_back_its_pass_slot(self, revel_user: RevelUser, series_pass: SeriesPass) -> None:
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
            price_paid=series_pass.price,
        )
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=4)

        delete_user_account(str(revel_user.pk))

        assert _sold(SeriesPass, series_pass.pk) == 3

    def test_an_active_held_pass_and_its_tickets_keep_their_slots(
        self, revel_user: RevelUser, series_pass: SeriesPass, event: Event, ticket_tier: TicketTier
    ) -> None:
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=series_pass.price,
        )
        Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Pass Holder",
        )
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=4)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=4)

        delete_user_account(str(revel_user.pk))

        assert _sold(SeriesPass, series_pass.pk) == 4
        assert _sold(TicketTier, ticket_tier.pk) == 4

    def test_other_accounts_keep_their_slots(
        self, revel_user: RevelUser, member_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(member_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=1)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 1
