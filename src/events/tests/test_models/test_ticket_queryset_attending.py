"""Tests for TicketQuerySet.attending() (#1278)."""

import pytest

from accounts.models import RevelUser
from events.models import Event, Ticket

pytestmark = pytest.mark.django_db


def test_attending_includes_only_active_and_checked_in(event: Event, django_user_model: type[RevelUser]) -> None:
    """attending() keeps ACTIVE and CHECKED_IN and drops PENDING and CANCELLED, covering all statuses."""
    tier = event.ticket_tiers.first()
    assert tier is not None
    by_status: dict[str, Ticket] = {}
    for i, status in enumerate(Ticket.TicketStatus):
        user = django_user_model.objects.create_user(username=f"u{i}", email=f"u{i}@example.com", password="pass")
        by_status[status.value] = Ticket.objects.create(
            event=event, user=user, tier=tier, guest_name=f"G{i}", status=status
        )

    attending_ids = set(Ticket.objects.attending().filter(event=event).values_list("id", flat=True))

    expected = {by_status[Ticket.TicketStatus.ACTIVE].id, by_status[Ticket.TicketStatus.CHECKED_IN].id}
    assert attending_ids == expected
    assert set(by_status) == {s.value for s in Ticket.TicketStatus}
