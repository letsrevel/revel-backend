import typing as t
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch, Q
from django.utils import timezone

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TagAssignment, TaggableMixin, TimeStampedModel

from .event_series import EventSeries
from .mixins import (
    LocationMixin,
    LogoCoverValidationMixin,
    ResourceVisibility,
    SlugFromNameMixin,
    VisibilityMixin,
)
from .organization import Organization, OrganizationMember
from .ticket import _get_payment_default_expiry  # noqa: F401  # Re-export for migration compatibility
from .venue import Venue


class EventQuerySet(models.QuerySet["Event"]):
    def with_tags(self) -> t.Self:
        """Prefetch tags and related tag objects for max performance."""
        return self.prefetch_related(
            Prefetch(
                "tags",  # the GenericRelation on Event
                queryset=TagAssignment.objects.select_related("tag"),
                to_attr="prefetched_tagassignments",  # Optional: if you want to use a custom attribute
            )
        )

    def with_city(self) -> t.Self:
        """Select the city as well."""
        return self.select_related("city")

    def with_organization(self) -> t.Self:
        """Returns a queryset prefetching an organization and its members."""
        return self.select_related("organization").prefetch_related("organization__staff_members")

    def with_venue(self) -> t.Self:
        """Select the venue (without sectors/seats) to avoid N+1."""
        return self.select_related("venue")

    def for_user(
        self, user: RevelUser | AnonymousUser, include_past: bool = False, allowed_ids: list[UUID] | None = None
    ) -> t.Self:
        """Get the queryset based on the user, using an efficient subquery strategy.

        Membership status handling:
        - BANNED users: Cannot see events from organizations where they are banned, even if public
        - CANCELLED users: Treated as if they have no membership
        - PAUSED/ACTIVE users: Can see events based on visibility rules
        """
        from .invitation import EventInvitation
        from .rsvp import EventRSVP
        from .ticket import Ticket

        base_qs = self.select_related("organization", "event_series", "venue")

        is_allowed_special = Q(id__in=allowed_ids) if allowed_ids else Q()

        if not include_past:
            today = timezone.now().date()
            base_qs = base_qs.filter(Q(start__date__gte=today) | Q(start__isnull=True) | is_allowed_special)

        if user.is_superuser or user.is_staff:
            return base_qs

        if user.is_anonymous:
            return base_qs.filter(
                Q(visibility=Event.Visibility.PUBLIC, status__in=[Event.EventStatus.OPEN, Event.EventStatus.CLOSED])
                | is_allowed_special
            )

        # --- Get banned and blacklisted organization IDs ---
        # Users banned/blacklisted from an organization cannot see its events, even if public
        from events.service.blacklist_service import get_hard_blacklisted_org_ids

        banned_org_ids = OrganizationMember.objects.filter(
            user=user, status=OrganizationMember.MembershipStatus.BANNED
        ).values_list("organization_id", flat=True)

        blacklisted_org_ids = get_hard_blacklisted_org_ids(user)

        # Combine banned and blacklisted org IDs
        excluded_org_ids = set(banned_org_ids) | set(blacklisted_org_ids)

        # --- Subquery Strategy ---
        # 1. Get IDs of all non-public events this user has a specific relationship with.

        # Events they are invited to
        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)

        # Events where they are a valid member of the organization (not cancelled, not banned)
        member_org_ids = (
            OrganizationMember.objects.for_visibility().filter(user=user).values_list("organization_id", flat=True)
        )
        member_event_ids = self.filter(
            visibility=Event.Visibility.MEMBERS_ONLY, organization_id__in=member_org_ids
        ).values_list("id", flat=True)

        # Combine these IDs into a single set
        ticket_event_ids = Ticket.objects.filter(user=user).values_list("event_id", flat=True)
        rsvp_event_ids = EventRSVP.objects.filter(user=user).values_list("event_id", flat=True)

        # Combine all these IDs into a single set
        allowed_non_public_ids = (
            set(invited_event_ids)
            | set(member_event_ids)
            | set(ticket_event_ids)
            | set(rsvp_event_ids)
            | set(allowed_ids or [])  # allow specific extra ids (e.g., when an EventToken is used).
        )

        # 2. Build the final query
        is_owner_or_staff = Q(organization__owner=user) | Q(organization__staff_members=user)
        is_public = Q(visibility=Event.Visibility.PUBLIC) & ~Q(organization_id__in=excluded_org_ids)
        is_allowed_non_public = Q(id__in=list(allowed_non_public_ids))

        # Users see events if they are public (and not banned), if they are staff/owner,
        # or if they have a specific permission (invite/member)
        final_qs = base_qs.filter(is_public | is_owner_or_staff | is_allowed_non_public)

        # Only staff/owners can see drafts
        if not (user.is_staff or user.is_superuser):
            final_qs = final_qs.exclude(~is_owner_or_staff & Q(status=Event.EventStatus.DRAFT))

        return final_qs.distinct()


class EventManager(models.Manager["Event"]):
    def get_queryset(self) -> EventQuerySet:
        """Get base queryset for events."""
        return EventQuerySet(self.model, using=self._db)

    def with_organization(self) -> EventQuerySet:
        """Returns a queryset prefetching an organization and its members."""
        return self.get_queryset().with_organization()

    def with_city(self) -> EventQuerySet:
        """Returns a queryset selecting the related city for the events."""
        return self.get_queryset().with_city()

    def with_tags(self) -> EventQuerySet:
        """Returns a queryset prefetching the tags."""
        return self.get_queryset().with_tags()

    def with_venue(self) -> EventQuerySet:
        """Returns a queryset selecting the related venue (without sectors/seats)."""
        return self.get_queryset().with_venue()

    def full(self) -> EventQuerySet:
        """Returns a queryset prefetching the full events."""
        return self.get_queryset().with_organization().with_city().with_tags().with_venue()

    def for_user(
        self, user: RevelUser | AnonymousUser, include_past: bool = False, allowed_ids: list[UUID] | None = None
    ) -> EventQuerySet:
        """Get the queryset based on the user."""
        return self.get_queryset().for_user(user, include_past=include_past, allowed_ids=allowed_ids)


class Event(
    SlugFromNameMixin, TimeStampedModel, VisibilityMixin, LocationMixin, LogoCoverValidationMixin, TaggableMixin
):
    # Slug uniqueness is scoped to organization
    slug_scope_field = "organization"

    class EventType(models.TextChoices):
        PUBLIC = "public"
        PRIVATE = "private"
        MEMBERS_ONLY = "members-only"

    class EventStatus(models.TextChoices):
        OPEN = "open"
        CLOSED = "closed"
        DRAFT = "draft"
        CANCELLED = "cancelled"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="events")
    status = models.CharField(choices=EventStatus.choices, max_length=10, default=EventStatus.DRAFT)
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)
    description = MarkdownField(blank=True, null=True)
    invitation_message = MarkdownField(
        blank=True,
        null=True,
        help_text="Invitation message to override the one automatically generated using name and description.",
    )
    event_type = models.CharField(choices=EventType.choices, max_length=20, db_index=True, default=EventType.PRIVATE)
    event_series = models.ForeignKey(
        EventSeries, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    max_attendees = models.PositiveIntegerField(default=0)
    waitlist_open = models.BooleanField(default=False)
    waitlist = models.ManyToManyField(  # type: ignore[var-annotated]
        settings.AUTH_USER_MODEL, related_name="waitlist", blank=True, through="EventWaitList"
    )
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(db_index=True)
    rsvp_before = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="RSVP deadline for events that do not require tickets"
    )
    check_in_starts_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When check-in opens for this event"
    )
    check_in_ends_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When check-in closes for this event"
    )
    requires_ticket = models.BooleanField(default=True)  # If False, managed via RSVPs
    requires_full_profile = models.BooleanField(
        default=False, help_text="Whether this event requires full profile information (profile pic, name, pronouns)"
    )
    potluck_open = models.BooleanField(default=False)
    accept_invitation_requests = models.BooleanField(default=False)
    apply_before = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="Deadline for submitting invitation requests or questionnaires"
    )

    @property
    def effective_apply_deadline(self) -> datetime:
        """Return the apply deadline, falling back to event start if not set."""
        return self.apply_before or self.start

    can_attend_without_login = models.BooleanField(
        default=True, help_text="Allow users to RSVP or purchase tickets without creating an account"
    )
    address_visibility = models.CharField(
        choices=ResourceVisibility.choices,
        max_length=20,
        default=ResourceVisibility.PUBLIC,
        help_text="Controls who can see the event address. Uses same rules as resource visibility.",
    )

    attendee_count = models.PositiveIntegerField(default=0, editable=False)
    max_tickets_per_user = models.PositiveIntegerField(
        default=1,
        null=True,
        blank=True,
        help_text="Maximum tickets a user can purchase for this event. Null = unlimited.",
    )

    venue = models.ForeignKey(
        Venue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        help_text="Optional venue for this event.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_organization_slug"),
        ]
        indexes = [
            models.Index(fields=["visibility", "status"], name="idx_visibility_status"),
            models.Index(fields=["organization", "status"], name="idx_org_status"),
            models.Index(fields=["organization", "visibility"], name="idx_org_visibility"),
            models.Index(fields=["event_type", "start"], name="idx_type_start"),
            models.Index(fields=["status", "start"], name="idx_status_start"),
            models.Index(fields=["visibility", "organization"], name="idx_visibility_organization"),
        ]
        ordering = ["start"]

    objects = EventManager()

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to set default end date if not provided."""
        if self.start and not self.end:
            self.end = self.start + timedelta(days=1)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"

    @property
    def effective_capacity(self) -> int:
        """Get the effective capacity considering both max_attendees and venue capacity.

        Returns the minimum of max_attendees and venue.capacity when both are set,
        or whichever is set if only one exists. Returns 0 (unlimited) if neither is set.

        This is a soft limit that can be overridden by invitations with
        overrides_max_attendees=True. For hard limits, see sector capacity.

        Note:
            Ensure the venue is prefetched (via select_related or with_venue())
            before accessing this property to avoid N+1 queries.

        Returns:
            Effective capacity as int. 0 means unlimited.
        """
        capacities = [cap for cap in [self.max_attendees, self.venue.capacity if self.venue else None] if cap]
        return min(capacities) if capacities else 0

    def can_user_see_address(self, user: RevelUser | AnonymousUser) -> bool:
        """Check if the user can see the event address based on address_visibility.

        Uses the same visibility rules as ResourceVisibility:
        - PUBLIC: Everyone can see
        - PRIVATE: Invited users, ticket holders, or RSVPs
        - MEMBERS_ONLY: Organization members
        - STAFF_ONLY: Organization staff/owners
        - ATTENDEES_ONLY: Only ticket holders or RSVPs (not just invited)

        Results are cached on the instance to avoid repeated queries when called
        multiple times (e.g., from schema resolution). Cache resets when instance
        is re-fetched from DB.

        Args:
            user: The user to check access for.

        Returns:
            True if the user can see the address, False otherwise.
        """
        # Instance-level cache keyed by user id
        cache_key = getattr(user, "id", None)
        if not hasattr(self, "_address_visibility_cache"):
            self._address_visibility_cache: dict[t.Any, bool] = {}
        if cache_key in self._address_visibility_cache:
            return self._address_visibility_cache[cache_key]

        result = self._compute_can_user_see_address(user)
        self._address_visibility_cache[cache_key] = result
        return result

    def _compute_can_user_see_address(self, user: RevelUser | AnonymousUser) -> bool:
        """Compute address visibility without caching."""
        from .invitation import EventInvitation
        from .rsvp import EventRSVP
        from .ticket import Ticket

        # Staff/superusers always see everything
        if user.is_superuser or user.is_staff:
            return True

        # PUBLIC is visible to everyone
        if self.address_visibility == ResourceVisibility.PUBLIC:
            return True

        # Anonymous users can only see PUBLIC addresses
        if user.is_anonymous:
            return False

        # Check organization roles
        is_owner = self.organization.owner_id == user.id
        # Use .all() to leverage prefetched data when available (from with_organization())
        # instead of .filter().exists() which always creates a new query
        is_staff_member = any(m.id == user.id for m in self.organization.staff_members.all())

        # STAFF_ONLY: Only staff/owners
        if self.address_visibility == ResourceVisibility.STAFF_ONLY:
            return is_owner or is_staff_member

        # Staff and owners can see everything
        if is_owner or is_staff_member:
            return True

        # Check if user is an organization member
        is_org_member = (
            OrganizationMember.objects.for_visibility().filter(user=user, organization=self.organization).exists()
        )

        # MEMBERS_ONLY: Organization members
        if self.address_visibility == ResourceVisibility.MEMBERS_ONLY:
            return is_org_member

        # Check event relationships
        has_ticket = Ticket.objects.filter(user=user, event=self).exclude(status=Ticket.TicketStatus.CANCELLED).exists()
        has_rsvp = EventRSVP.objects.filter(user=user, event=self, status=EventRSVP.RsvpStatus.YES).exists()
        has_invitation = EventInvitation.objects.filter(user=user, event=self).exists()

        # ATTENDEES_ONLY: Only ticket holders or RSVPs (not just invited)
        if self.address_visibility == ResourceVisibility.ATTENDEES_ONLY:
            return has_ticket or has_rsvp

        # PRIVATE: Invited users, ticket holders, or RSVPs
        if self.address_visibility == ResourceVisibility.PRIVATE:
            return has_ticket or has_rsvp or has_invitation

        return False

    def attendees(self, viewer: RevelUser) -> models.QuerySet[RevelUser]:
        """Return attendees based on who wants to see them."""
        from .rsvp import EventRSVP
        from .ticket import Ticket

        # Use .all() to leverage prefetched data when available (from with_organization())
        is_staff_member = any(m.id == viewer.id for m in self.organization.staff_members.all())
        if viewer.is_superuser or viewer.is_staff or self.organization.owner_id == viewer.id or is_staff_member:
            return RevelUser.objects.filter(
                Q(tickets__event=self, tickets__status=Ticket.TicketStatus.ACTIVE)
                | Q(rsvps__event=self, rsvps__status=EventRSVP.RsvpStatus.YES)
            ).distinct()
        return RevelUser.objects.filter(
            id__in=AttendeeVisibilityFlag.objects.filter(event=self, user=viewer, is_visible=True).values_list(
                "target_id", flat=True
            )
        )

    def clean(self) -> None:
        """Enforce same ownership of organization and validate time windows."""
        super().clean()
        if self.event_series and self.event_series.organization_id != self.organization_id:
            raise DjangoValidationError(
                {
                    "event_series": "Event series must belong to the same organization as the event.",
                }
            )
        if self.end and self.end < self.start:
            raise DjangoValidationError({"end": "End date must be after start date."})

        # Validate check-in window
        if self.check_in_starts_at and self.check_in_ends_at:
            if self.check_in_ends_at <= self.check_in_starts_at:
                raise DjangoValidationError(
                    {"check_in_ends_at": "Check-in end time must be after check-in start time."}
                )

        # Validate venue belongs to the same organization
        venue = self.venue
        if venue and venue.organization_id != self.organization_id:
            raise DjangoValidationError({"venue": "Venue must belong to the same organization as the event."})

    def is_check_in_open(self) -> bool:
        """Check if check-in is currently open for this event."""
        now = timezone.now()
        if not self.status == self.EventStatus.OPEN:
            return False

        return (self.check_in_starts_at or self.start) <= now <= (self.check_in_ends_at or self.end)

    def ics(self) -> bytes:
        """Generates an iCalendar (.ics) file for this event.

        Returns:
            The .ics file content as bytes.
        """
        from ics import Calendar
        from ics import Event as ICSEvent

        c = Calendar()
        e = ICSEvent()

        e.name = self.name
        e.begin = self.start
        e.end = self.end
        e.location = self.address or (self.city.name if self.city else "See event details")
        # e.description = ticket.event.description or f"Your ticket for {ticket.event.name}."
        e.uid = f"{self.id}@letsrevel.io"  # Unique ID for the calendar event

        c.events.add(e)

        # The ics library returns a string, so we encode it to bytes
        return t.cast(bytes, c.serialize().encode("utf-8"))


class EventWaitList(TimeStampedModel):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "user"], name="unique_event_waitlist")]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id} on waitlist for {self.event_id}"


class AttendeeVisibilityFlag(TimeStampedModel):
    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="visible_attendees")
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    target = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="visible_to")

    is_visible = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event", "target"],
                name="unique_attendee_visibility_constraint",
            )
        ]
        indexes = [
            models.Index(fields=["user", "event"]),
            models.Index(fields=["event", "target"]),
        ]

    def __str__(self) -> str:
        visibility = "visible" if self.is_visible else "hidden"
        return f"{self.target_id} {visibility} to {self.user_id}"
