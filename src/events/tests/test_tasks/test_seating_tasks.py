"""Sweeper deletes only expired holds."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, SeatHold, VenueSeat
from events.tasks.seating import cleanup_expired_seat_holds

pytestmark = pytest.mark.django_db


@pytest.fixture
def revel_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="seat_holder", email="seat_holder@example.com", password="pw")


def test_sweeper_deletes_only_expired(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seats[0], user=revel_user, acquired_at=now, expires_at=now - timedelta(minutes=1)
    )
    SeatHold.objects.create(
        event=event, seat=seats[1], user=revel_user, acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    deleted = cleanup_expired_seat_holds()
    assert deleted == 1
    assert SeatHold.objects.count() == 1
