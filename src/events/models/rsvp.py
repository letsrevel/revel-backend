import typing as t
from uuid import UUID

from django.conf import settings
from django.contrib.gis.db import models
from django.db.models import Prefetch

from common.models import TimeStampedModel

from .organization import OrganizationMember


class EventRSVPQuerySet(models.QuerySet["EventRSVP"]):
    """Custom queryset for EventRSVP model."""

    def with_user(self) -> t.Self:
        """Select the related user."""
        return self.select_related("user")

    def with_event_details(self) -> t.Self:
        """Prefetch event with venue for MinimalEventSchema."""
        return self.select_related("event", "event__venue")

    def with_org_membership(self, organization_id: UUID) -> t.Self:
        """Prefetch user's membership for a specific organization."""
        return self.prefetch_related(
            Prefetch(
                "user__organization_memberships",
                queryset=OrganizationMember.objects.filter(organization_id=organization_id).select_related("tier"),
                to_attr="org_membership_list",
            )
        )


class EventRSVPManager(models.Manager["EventRSVP"]):
    """Custom manager for EventRSVP."""

    def get_queryset(self) -> EventRSVPQuerySet:
        """Get base queryset."""
        return EventRSVPQuerySet(self.model, using=self._db)

    def with_user(self) -> EventRSVPQuerySet:
        """Returns a queryset with the user selected."""
        return self.get_queryset().with_user()

    def with_event_details(self) -> EventRSVPQuerySet:
        """Returns a queryset with event and venue prefetched."""
        return self.get_queryset().with_event_details()

    def with_org_membership(self, organization_id: UUID) -> EventRSVPQuerySet:
        """Returns a queryset with org membership prefetched."""
        return self.get_queryset().with_org_membership(organization_id)


class EventRSVP(TimeStampedModel):
    class RsvpStatus(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        MAYBE = "maybe", "Maybe"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="rsvps")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rsvps")
    status = models.CharField(
        max_length=20, choices=RsvpStatus.choices, default=None, null=True, blank=True, db_index=True
    )

    objects = EventRSVPManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_user",
            )
        ]

    def __str__(self) -> str:
        return f"RSVP: {self.user_id} -> {self.event_id} ({self.status})"
