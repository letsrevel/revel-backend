"""The activation backfill does not issue again a pass ticket that someone cancelled (#1565).

``backfill_missing_tickets`` runs when a held pass becomes ACTIVE. A ticket of the pass that a person or an
event cancellation cancelled (a ``cancellation_source`` is set) counts as "ticketed", so no new ticket is
issued for its event. A ticket cancelled with no source (the ``payment_intent.canceled`` webhook of an
abandoned checkout) does not count, so the backfill still issues it, as before.
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
    ],
)
def test_a_ticket_cancelled_by_someone_is_not_issued_again(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, source: CancellationSource
) -> None:
    held_pass = _pending_offline_pass(series_pass, revel_user)
    Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).update(
        status=Ticket.TicketStatus.CANCELLED, cancellation_source=source
    )

    _confirm(held_pass)

    assert _tickets_for(held_pass, three_upcoming[0]) == [Ticket.TicketStatus.CANCELLED]
    assert _tickets_for(held_pass, three_upcoming[1]) == [Ticket.TicketStatus.ACTIVE]


def test_a_ticket_cancelled_with_no_source_is_issued_again(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    """An abandoned checkout cancels with no source: the activation replaces that ticket, as before #1565."""
    held_pass = _pending_offline_pass(series_pass, revel_user)
    Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).update(
        status=Ticket.TicketStatus.CANCELLED, cancellation_source=""
    )

    _confirm(held_pass)

    assert sorted(_tickets_for(held_pass, three_upcoming[0])) == sorted(
        [Ticket.TicketStatus.CANCELLED, Ticket.TicketStatus.ACTIVE]
    )
