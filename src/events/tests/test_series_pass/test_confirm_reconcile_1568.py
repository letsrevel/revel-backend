"""An offline series pass confirm that loses its rebuild is recovered by the sweep (#1568).

The confirm activates the PENDING tickets with ``bulk_update``, which skips ``auto_now``. It now stamps
``updated_at`` by hand, so ``events.reconcile_attendee_counts`` finds the event even when the tickets were
created days before the confirm.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks import reconcile_attendee_counts

pytestmark = pytest.mark.django_db


def test_a_lost_rebuild_after_an_offline_confirm_is_recovered(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
    series_pass.save(update_fields=["payment_method"])
    held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    three_days_ago = timezone.now() - timedelta(days=3)
    Ticket.objects.filter(held_pass=held_pass).update(updated_at=three_days_ago, created_at=three_days_ago)

    with (
        patch("events.service.series_pass_service.send_series_pass_purchased"),
        patch("events.service.series_pass_service.build_attendee_visibility_flags.delay"),  # the lost rebuild
    ):
        series_pass_service.confirm_held_pass_payment(held_pass)
    # The confirm stamped updated_at: bulk_update alone leaves it at the purchase, out of the sweep window.
    stamped = Ticket.objects.filter(held_pass=held_pass).values_list("updated_at", flat=True)
    assert stamped and all(value > timezone.now() - timedelta(minutes=1) for value in stamped)
    # Past the sweep's grace period, as when the sweep runs 15 minutes later.
    Ticket.objects.filter(held_pass=held_pass).update(updated_at=timezone.now() - timedelta(minutes=10))

    with patch("events.tasks.attendees.build_attendee_visibility_flags.delay") as delay:
        dispatched = reconcile_attendee_counts()

    assert dispatched == len(three_upcoming)
    assert {call.args[0] for call in delay.call_args_list} == {str(event.pk) for event in three_upcoming}
