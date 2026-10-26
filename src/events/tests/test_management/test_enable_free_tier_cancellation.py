"""Tests for the ``enable_free_tier_cancellation`` management command."""

from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import CommandError, call_command

from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db


def _tier(event: Event, name: str, payment_method: str) -> TicketTier:
    return TicketTier.objects.create(event=event, name=name, payment_method=payment_method)


def test_enables_only_free_tiers_of_the_event(event: Event, public_event: Event) -> None:
    free = _tier(event, "Free", TicketTier.PaymentMethod.FREE)
    paid = _tier(event, "Paid", TicketTier.PaymentMethod.ONLINE)
    other_event_free = _tier(public_event, "Free", TicketTier.PaymentMethod.FREE)

    out = StringIO()
    call_command("enable_free_tier_cancellation", str(event.pk), stdout=out)

    for tier in (free, paid, other_event_free):
        tier.refresh_from_db()
    assert free.allow_user_cancellation is True
    assert paid.allow_user_cancellation is False
    assert other_event_free.allow_user_cancellation is False
    assert "1 free tier(s)" in out.getvalue()


def test_dry_run_writes_nothing(event: Event) -> None:
    free = _tier(event, "Free", TicketTier.PaymentMethod.FREE)

    out = StringIO()
    call_command("enable_free_tier_cancellation", str(event.pk), "--dry-run", stdout=out)

    free.refresh_from_db()
    assert free.allow_user_cancellation is False
    assert "[dry-run]" in out.getvalue()


def test_unknown_event_raises() -> None:
    with pytest.raises(CommandError, match="not found"):
        call_command("enable_free_tier_cancellation", str(uuid4()))
