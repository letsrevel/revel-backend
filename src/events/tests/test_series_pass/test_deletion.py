"""Deletion semantics for ``Ticket.held_pass`` (RESTRICT, not PROTECT).

``on_delete=RESTRICT`` lets a cascading ``user.delete()`` succeed — the user's own
tickets are already being cascade-deleted via ``Ticket.user``, which satisfies the
restriction — while a bare ``held_pass.delete()`` (no wider cascade in play) is
still blocked, preserving the purchase/attendance audit trail. See F1 in the
review-fix report for the full trace of why ``PROTECT`` broke GDPR account
deletion (``accounts.tasks.delete_user_account``) for any user who ever held a
series pass.
"""

import pytest
from django.db.models import RestrictedError

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier

pytestmark = pytest.mark.django_db


class TestHeldPassDeletionSemantics:
    def test_user_delete_succeeds_with_active_held_pass_and_materialized_ticket(
        self,
        revel_user: RevelUser,
        series_pass: SeriesPass,
        event: Event,
        ticket_tier: TicketTier,
    ) -> None:
        """A cascading ``user.delete()`` must succeed, taking the held pass and its
        materialized ticket with it — this is the GDPR-deletion path."""
        held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=series_pass.price,
        )
        ticket = Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Pass Holder",
        )

        revel_user.delete()

        assert not HeldSeriesPass.objects.filter(pk=held_pass.pk).exists()
        assert not Ticket.objects.filter(pk=ticket.pk).exists()

    def test_bare_held_pass_delete_with_live_ticket_raises_restricted_error(
        self,
        revel_user: RevelUser,
        series_pass: SeriesPass,
        event: Event,
        ticket_tier: TicketTier,
    ) -> None:
        """Deleting a ``HeldSeriesPass`` directly (no wider cascade) must still be
        refused — the referencing ticket is not otherwise being deleted, so RESTRICT
        behaves like PROTECT here and preserves the audit trail."""
        held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=series_pass.price,
        )
        Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Pass Holder",
        )

        with pytest.raises(RestrictedError):
            held_pass.delete()

        assert HeldSeriesPass.objects.filter(pk=held_pass.pk).exists()
