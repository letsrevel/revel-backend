import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.db.models import Q

from accounts.models import RevelUser
from common.fields import MarkdownField, ProtectedFileField
from common.models import TimeStampedModel

from .. import exceptions
from .event import Event
from .event_series import EventSeries
from .invitation import EventInvitation
from .mixins import ResourceVisibility, VisibilityMixin
from .organization import Organization
from .rsvp import EventRSVP
from .ticket import Ticket


class AdditionalResourceQuerySet(models.QuerySet["AdditionalResource"]):
    def with_related(self) -> t.Self:
        """Prefetch related fields to prevent N+1 queries."""
        return self.select_related("organization").prefetch_related("event_series", "events")

    def for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Get the queryset of resources visible to the user.

        This method applies a multi-layered filter:
        1. It first determines which organizations the user can see, reusing the
           optimized `Organization.objects.for_user()` logic as a base filter.
        2. It then applies visibility rules based on the user's role (staff, member)
           for non-private resources (`PUBLIC`, `MEMBERS_ONLY`, `STAFF_ONLY`).
        3. Crucially, it adds a specific check for `PRIVATE` resources, making them visible
           *only if* the user has a direct relationship (invitation, ticket, or RSVP)
           to an event linked to that resource.
        4. For `ATTENDEES_ONLY` resources, only users with tickets or RSVPs (not just invitations)
           can see them.
        """
        # --- Fast paths for special users ---
        qs = self.all()
        if user.is_superuser or user.is_staff:
            return qs

        # --- Anonymous User ---
        if user.is_anonymous:
            # Anonymous users can only see PUBLIC resources in PUBLIC organizations.
            # The visible_org_ids check already handles the org's public status.
            return qs.filter(visibility=ResourceVisibility.PUBLIC)

        # --- Authenticated User ---
        # A user's visibility is the sum of several permissions. We build a
        # query that combines them using OR (`|`).

        # 1. Visibility based on the user's role in the organization
        #    (for non-private resources).
        is_owner = Q(organization__owner=user)
        is_staff_member = Q(organization__staff_members=user)
        is_org_member = Q(organization__members=user)

        # Staff and owners see everything up to 'staff-only'.
        role_based_q = is_owner | is_staff_member
        # Regular members see 'members-only' and 'public' resources.
        role_based_q |= is_org_member & Q(visibility=ResourceVisibility.MEMBERS_ONLY)
        # Any authenticated user with access to the org can see public resources.
        role_based_q |= Q(visibility=ResourceVisibility.PUBLIC)

        # 2. Visibility for PRIVATE resources based on event relationship.
        # Gather all event IDs the user is directly connected to.
        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)
        # Only consider valid tickets (exclude cancelled ones)
        ticketed_event_ids = (
            Ticket.objects.filter(user=user)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .values_list("event_id", flat=True)
        )
        rsvpd_event_ids = EventRSVP.objects.filter(user=user, status=EventRSVP.RsvpStatus.YES).values_list(
            "event_id", flat=True
        )

        related_event_ids = set(invited_event_ids) | set(ticketed_event_ids) | set(rsvpd_event_ids)

        private_resources_q = Q()
        if related_event_ids:
            # If the user is connected to any events, build the query to find
            # private resources linked to those specific events.
            private_resources_q = Q(visibility=ResourceVisibility.PRIVATE, events__id__in=list(related_event_ids))

        # 3. Visibility for ATTENDEES_ONLY resources based on ticket/RSVP only (not invitation).
        # Only users with actual tickets or RSVPs can see these, not just invited users.
        attendee_event_ids = set(ticketed_event_ids) | set(rsvpd_event_ids)
        attendees_only_resources_q = Q()
        if attendee_event_ids:
            attendees_only_resources_q = Q(
                visibility=ResourceVisibility.ATTENDEES_ONLY, events__id__in=list(attendee_event_ids)
            )

        # 4. Combine the role-based, private-event-based, and attendees-only queries.
        # A resource is visible if it matches EITHER the role criteria OR the private event criteria
        # OR the attendees-only criteria.
        final_q = role_based_q | private_resources_q | attendees_only_resources_q

        return qs.filter(final_q).distinct()


class AdditionalResourceManager(models.Manager["AdditionalResource"]):
    def get_queryset(self) -> AdditionalResourceQuerySet:
        """Get the base AdditionalResource queryset."""
        return AdditionalResourceQuerySet(self.model, using=self._db)

    def with_related(self) -> AdditionalResourceQuerySet:
        """Prefetch related fields."""
        return self.get_queryset().with_related()

    def for_user(self, user: RevelUser | AnonymousUser) -> AdditionalResourceQuerySet:
        """Return only AdditionalResources that are visible to the given user."""
        return self.get_queryset().for_user(user)


class AdditionalResource(TimeStampedModel, VisibilityMixin):
    class ResourceTypes(models.TextChoices):
        FILE = "file"
        LINK = "link"
        TEXT = "text"

    resource_type = models.CharField(
        choices=ResourceTypes.choices,
        max_length=255,
        db_index=True,
    )
    # Override visibility field to use ResourceVisibility instead of base Visibility
    visibility = models.CharField(
        choices=ResourceVisibility.choices, max_length=20, db_index=True, default=ResourceVisibility.PRIVATE
    )
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="additional_resources")
    display_on_organization_page = models.BooleanField(
        default=True, help_text="Whether the resource should be displayed on organization pages."
    )
    event_series = models.ManyToManyField(EventSeries, related_name="additional_resources", blank=True)
    events = models.ManyToManyField(Event, related_name="additional_resources", blank=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    description = MarkdownField(null=True, blank=True)
    file = ProtectedFileField(upload_to="file", null=True, blank=True)
    link = models.URLField(null=True, blank=True)
    text = MarkdownField(null=True, blank=True)

    objects = AdditionalResourceManager()

    def __str__(self) -> str:
        return f"{self.name or 'Unnamed'} ({self.resource_type})"

    def clean(self) -> None:
        """Override the clean method."""
        super().clean()

        must_field = self.resource_type
        must_not_fields = [f for f in self.ResourceTypes.__members__.values() if f != must_field]
        errors = {}
        if not bool(getattr(self, must_field)) and self.resource_type != self.ResourceTypes.FILE:
            # we need to exclude file because of the safe_save_upload_file flow, which is going to set the file itself
            errors[must_field] = f"Must be set for resource type {self.resource_type!r}"

        for field in must_not_fields:
            if bool(getattr(self, field)):
                errors[field] = f"Must not be set for resource type {self.resource_type!r}"
        if errors:
            raise exceptions.InvalidResourceStateError(errors)

    class Meta:
        ordering = ["-created_at"]
