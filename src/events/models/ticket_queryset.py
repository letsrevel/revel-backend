import typing as t
from uuid import UUID

from django.contrib.gis.db import models
from django.db.models import Prefetch

from .membership import OrganizationMember

if t.TYPE_CHECKING:
    from .ticket import Ticket  # noqa: F401  (resolves the "Ticket" string annotations)


class TicketQuerySet(models.QuerySet["Ticket"]):
    """Custom queryset for Ticket model with common prefetch patterns."""

    def with_event(self) -> t.Self:
        """Select the related event and its organization."""
        return self.select_related("event", "event__organization")

    def with_tier(self) -> t.Self:
        """Select the related tier with venue/sector and city for N+1 prevention."""
        return self.select_related(
            "tier",
            "tier__venue",
            "tier__venue__city",
            "tier__sector",
        )

    def with_seat(self) -> t.Self:
        """Select the related seat for assigned seating."""
        return self.select_related("seat")

    def with_user(self) -> t.Self:
        """Select the related user."""
        return self.select_related("user")

    def with_payment(self) -> t.Self:
        """Select the related payment (OneToOne reverse relation)."""
        return self.select_related("payment")

    def full(self) -> t.Self:
        """Select all commonly needed related objects for user-facing ticket views."""
        return self.select_related(
            "event",
            "event__organization",
            "event__organization__city",
            "event__city",
            "event__venue",
            "event__venue__city",
            "tier",
            "tier__venue",
            "tier__venue__city",
            "tier__sector",
            "tier__event",
            "tier__event__organization",
            "tier__event__organization__city",
            "tier__event__city",
            "tier__event__venue__city",
            "venue",
            "venue__city",
            "seat",
            "user",
            "payment",
            "discount_code",
            "held_pass",
            "held_pass__series_pass",
        ).prefetch_related("tier__restricted_to_membership_tiers", "tier__excluded_membership_tiers")

    def attending(self) -> t.Self:
        """Tickets that count as attendance.

        This is the single definition of attendance: who is on the attendee list and in
        ``attendee_count``, and who counts as an attendee for visibility and attendee-only
        info. ACTIVE and CHECKED_IN count (checking in never drops anyone); PENDING and
        CANCELLED do not.
        """
        return self.filter(status__in=self.model.ATTENDING_STATUSES)

    def with_org_membership(self, organization_id: UUID) -> t.Self:
        """Prefetch user's membership for a specific organization."""
        return self.prefetch_related(
            Prefetch(
                "user__organization_memberships",
                queryset=OrganizationMember.objects.filter(organization_id=organization_id).select_related("tier"),
                to_attr="org_membership_list",
            )
        )


class TicketManager(models.Manager["Ticket"]):
    """Custom manager for Ticket with convenience methods for related object selection."""

    def get_queryset(self) -> TicketQuerySet:
        """Get base queryset."""
        return TicketQuerySet(self.model, using=self._db)

    def with_event(self) -> TicketQuerySet:
        """Returns a queryset with event and organization selected."""
        return self.get_queryset().with_event()

    def with_tier(self) -> TicketQuerySet:
        """Returns a queryset with tier, venue/sector, and city selected."""
        return self.get_queryset().with_tier()

    def with_seat(self) -> TicketQuerySet:
        """Returns a queryset with the seat selected."""
        return self.get_queryset().with_seat()

    def with_user(self) -> TicketQuerySet:
        """Returns a queryset with the user selected."""
        return self.get_queryset().with_user()

    def with_payment(self) -> TicketQuerySet:
        """Returns a queryset with the payment selected."""
        return self.get_queryset().with_payment()

    def full(self) -> TicketQuerySet:
        """Returns a queryset with all related objects selected."""
        return self.get_queryset().full()

    def attending(self) -> TicketQuerySet:
        """Returns a queryset of attending tickets (ACTIVE or CHECKED_IN; see ``TicketQuerySet.attending``)."""
        return self.get_queryset().attending()

    def with_org_membership(self, organization_id: UUID) -> TicketQuerySet:
        """Returns a queryset with org membership prefetched."""
        return self.get_queryset().with_org_membership(organization_id)
