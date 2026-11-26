"""The activation backfill does not issue a cancelled pass ticket again (#1565).

``backfill_missing_tickets`` runs when a held pass becomes ACTIVE. A ticket of the pass of any status counts as
"ticketed", as in the extension, so no new ticket is issued for an event whose pass ticket was cancelled. That
includes a cancel with no source, which a Django admin status edit leaves.
"""

from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.models.ticket import CancellationSource
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService

pytestmark = pytest.mark.django_db


def _pending_offline_pass(series_pass: SeriesPass, user: RevelUser) -> HeldSeriesPass:
    series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
    series_pass.save(update_fields=["payment_method"])
    held_pass = SeriesPassPurchaseService(series_pass, user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    return held_pass


def _confirm(held_pass: HeldSeriesPass) -> None:
    with patch("events.service.series_pass_service.send_series_pass_purchased"):
        series_pass_service.confirm_held_pass_payment(held_pass)


def _tickets_for(held_pass: HeldSeriesPass, event: Event) -> list[str]:
    return list(Ticket.objects.filter(held_pass=held_pass, event=event).values_list("status", flat=True))


@pytest.mark.parametrize(
    "source",
    [
        CancellationSource.ORGANIZER,
        CancellationSource.USER,
        CancellationSource.STRIPE_DASHBOARD,
        CancellationSource.EVENT_CANCELLATION,
        "",  # a Django admin status edit sets no source
    ],
)
def test_a_cancelled_pass_ticket_is_not_issued_again(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, source: str
) -> None:
    held_pass = _pending_offline_pass(series_pass, revel_user)
    Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).update(
        status=Ticket.TicketStatus.CANCELLED, cancellation_source=source
    )

    _confirm(held_pass)

    assert _tickets_for(held_pass, three_upcoming[0]) == [Ticket.TicketStatus.CANCELLED]
    assert _tickets_for(held_pass, three_upcoming[1]) == [Ticket.TicketStatus.ACTIVE]
