"""Per-event seat state: admin overrides and TTL cart holds."""

import typing as t

from django.db import models
from django.db.models import Q
from django.utils import timezone

from common.models import TimeStampedModel

from .event import Event
from .venue import VenueSeat


class EventSeatOverride(TimeStampedModel):
    """Sparse per-event seat exception: box-office hold or kill.

    Spec §1: status only in v1 — no per-event category override.
    """

    class OverrideStatus(models.TextChoices):
        HELD = "held", "Held (house/tech/promoter)"
        KILLED = "killed", "Killed (not sellable this event)"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seat_overrides", db_index=True)
    seat = models.ForeignKey(VenueSeat, on_delete=models.CASCADE, related_name="event_overrides", db_index=True)
    status = models.CharField(choices=OverrideStatus.choices, max_length=10, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "seat"], name="unique_event_seat_override")]
        indexes = [models.Index(fields=["event", "status"])]

    def __str__(self) -> str:
        return f"{self.event_id}/{self.seat_id}: {self.status}"


class SeatHoldQuerySet(models.QuerySet["SeatHold"]):
    def active(self) -> t.Self:
        """Unexpired holds. Correctness never depends on the sweeper."""
        return self.filter(expires_at__gt=timezone.now())


class SeatHold(TimeStampedModel):
    """Advisory TTL cart hold on a seat for one event.

    Acquisition is a takeover upsert (see service.seating.holds) — a time-predicate
    partial unique index is impossible in Postgres, so uniqueness is unconditional
    and expired rows are claimed in place.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seat_holds", db_index=True)
    seat = models.ForeignKey(VenueSeat, on_delete=models.CASCADE, related_name="holds", db_index=True)
    user = models.ForeignKey(
        "accounts.RevelUser", on_delete=models.CASCADE, related_name="seat_holds", null=True, blank=True
    )
    guest_session = models.CharField(max_length=64, blank=True, default="", db_index=True)
    acquired_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)

    objects = SeatHoldQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "seat"], name="unique_seathold_event_seat"),
            models.CheckConstraint(
                condition=(Q(user__isnull=False) & Q(guest_session="")) | (Q(user__isnull=True) & ~Q(guest_session="")),
                name="seathold_exactly_one_owner",
            ),
        ]
        indexes = [models.Index(fields=["event", "user"]), models.Index(fields=["event", "guest_session"])]

    @classmethod
    def owner_q(cls, user: t.Any, guest_session: str | None) -> Q:
        """Q matching holds owned by this identity (authenticated user OR guest session)."""
        if user is not None and getattr(user, "is_authenticated", False):
            return Q(user=user)
        return Q(guest_session=guest_session or "__none__")

    def __str__(self) -> str:
        return f"hold {self.event_id}/{self.seat_id} until {self.expires_at:%H:%M:%S}"
