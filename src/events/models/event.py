import typing as t
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MinValueValidator
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.functional import cached_property

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TagAssignment, TaggableMixin, TimeStampedModel

from .event_series import EventSeries
from .mixins import (
    LocationMixin,
    LogoCoverValidationMixin,
    ResourceVisibility,
    SlugFromNameMixin,
    TokenMixin,
    UserRequestMixin,
    VisibilityMixin,
)
from .organization import MembershipTier, Organization, OrganizationMember


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

    def for_user(
        self, user: RevelUser | AnonymousUser, include_past: bool = False, allowed_ids: list[UUID] | None = None
    ) -> t.Self:
        """Get the queryset based on the user, using an efficient subquery strategy.

        Membership status handling:
        - BANNED users: Cannot see events from organizations where they are banned, even if public
        - CANCELLED users: Treated as if they have no membership
        - PAUSED/ACTIVE users: Can see events based on visibility rules
        """
        base_qs = self.select_related("organization", "event_series")

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

        # --- Get banned organization IDs ---
        # Users banned from an organization cannot see its events, even if public
        banned_org_ids = OrganizationMember.objects.filter(
            user=user, status=OrganizationMember.MembershipStatus.BANNED
        ).values_list("organization_id", flat=True)

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
        is_public = Q(visibility=Event.Visibility.PUBLIC) & ~Q(organization_id__in=banned_org_ids)
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

    def full(self) -> EventQuerySet:
        """Returns a queryset prefetching the full events."""
        return self.get_queryset().with_organization().with_city().with_tags()

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
    potluck_open = models.BooleanField(default=False)
    accept_invitation_requests = models.BooleanField(default=False)
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

    def can_user_see_address(self, user: RevelUser | AnonymousUser) -> bool:
        """Check if the user can see the event address based on address_visibility.

        Uses the same visibility rules as ResourceVisibility:
        - PUBLIC: Everyone can see
        - PRIVATE: Invited users, ticket holders, or RSVPs
        - MEMBERS_ONLY: Organization members
        - STAFF_ONLY: Organization staff/owners
        - ATTENDEES_ONLY: Only ticket holders or RSVPs (not just invited)

        Args:
            user: The user to check access for.

        Returns:
            True if the user can see the address, False otherwise.
        """
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
        is_staff_member = self.organization.staff_members.filter(id=user.id).exists()

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
        if (
            viewer.is_superuser
            or viewer.is_staff
            or self.organization.owner_id == viewer.id
            or self.organization.staff_members.filter(id=viewer.id).exists()
        ):
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


DEFAULT_TICKET_TIER_NAME = "General Admission"


class TicketTierQuerySet(models.QuerySet["TicketTier"]):
    def for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Return ticket tiers visible to a given user, combining event and tier-level access.

        Membership status handling:
        - CANCELLED users: Treated as if they have no membership (no access to member-only tiers)
        - BANNED users: Inherit banned status from Event.for_user (won't see events at all)
        """
        qs = self.select_related("event", "event__organization")

        # --- Anonymous User ---
        if user.is_anonymous:
            return qs.filter(
                visibility=TicketTier.Visibility.PUBLIC,
                event__visibility=Event.Visibility.PUBLIC,
                event__status=Event.EventStatus.OPEN,
            )

        if user.is_superuser:
            return qs

        # --- Authenticated User ---
        # 1. Get all events this user is allowed to see. This is the source of truth.
        # Event.for_user already handles banned users (they won't see events from banned orgs)
        visible_event_ids = Event.objects.for_user(user, include_past=True).values_list("id", flat=True)

        # Base filter: only consider tiers on events the user can see.
        base_q = Q(event_id__in=visible_event_ids)

        # If user is owner/staff of the org, they can see all tiers on that event.
        is_owner_or_staff = Q(event__organization__owner=user) | Q(event__organization__staff_members=user)
        # Django staff are not the same as org staff, but we can treat them like superusers here.
        if user.is_staff:
            return qs.filter(base_q).distinct()

        # For regular users, apply tier-specific visibility rules ON TOP of visible events.
        is_public_tier = Q(visibility=TicketTier.Visibility.PUBLIC)

        # Only valid members (not cancelled, not banned) can see member-only tiers
        member_org_ids = (
            OrganizationMember.objects.for_visibility().filter(user=user).values_list("organization_id", flat=True)
        )
        is_member_tier = Q(
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            event__organization_id__in=member_org_ids,
        )

        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)
        is_private_tier = Q(visibility=TicketTier.Visibility.PRIVATE, event_id__in=invited_event_ids)

        # A regular user can see a tier if it's on a visible event AND...
        # ...it's a public tier, OR
        # ...it's a member tier and they are a valid member, OR
        # ...it's a private tier and they are invited.
        tier_visibility_q = is_public_tier | is_member_tier | is_private_tier

        # Staff/owners of other orgs might also see some of these events if public,
        # so we also explicitly include tiers for events they are staff/owner of.
        final_q = base_q & (tier_visibility_q | is_owner_or_staff)

        return qs.filter(final_q).distinct()


class TicketTierManager(models.Manager["TicketTier"]):
    def get_queryset(self) -> TicketTierQuerySet:
        """Get a QS for ticket tiers."""
        return TicketTierQuerySet(self.model, using=self._db).prefetch_related("restricted_to_membership_tiers")

    def for_user(self, user: RevelUser | AnonymousUser) -> TicketTierQuerySet:
        """Return ticket tiers visible to a given user, combining event and tier-level access."""
        return self.get_queryset().for_user(user)


class TicketTier(TimeStampedModel, VisibilityMixin):
    """The ticket tier.

    Please note:
    if an event is created with requires_ticket=True and no Tiers exist for that event,
    it will be created via signals.
    """

    # PurchasableBy defines who is allowed to purchase tickets for this tier.
    # Note: The choices here ('public', 'members', 'invited') do not directly align with the
    # choices for visibility ('public', 'private', 'members-only', 'staff-only').
    # This is intentional: visibility controls who can see the ticket tier, while purchasable_by
    # controls who can buy tickets. For example, a tier may be visible to all but only purchasable
    # by members or invitees. If you change these choices, ensure you update related business logic.
    class PurchasableBy(models.TextChoices):
        PUBLIC = "public", "General public"
        MEMBERS = "members", "Members only"
        INVITED = "invited", "Invitees only"
        INVITED_AND_MEMBERS = "invited_and_members", "Invited and Members only"

    class PaymentMethod(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        AT_THE_DOOR = "at_the_door", "At The Door"
        FREE = "free", "Free"

    class PriceType(models.TextChoices):
        FIXED = "fixed", "Fixed Price"
        PWYC = "pwyc", "Pay What You Can"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="ticket_tiers")
    name = models.CharField(max_length=255, db_index=True)
    visibility = models.CharField(
        choices=VisibilityMixin.Visibility.choices,
        default=VisibilityMixin.Visibility.PUBLIC,
        max_length=20,
        db_index=True,
    )
    payment_method = models.CharField(
        choices=PaymentMethod.choices,
        default=PaymentMethod.OFFLINE,
        max_length=20,
        db_index=True,
    )
    purchasable_by = models.CharField(
        choices=PurchasableBy.choices, max_length=20, db_index=True, default=PurchasableBy.PUBLIC
    )
    description = MarkdownField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    price_type = models.CharField(
        choices=PriceType.choices,
        default=PriceType.FIXED,
        max_length=10,
        db_index=True,
        help_text="Whether this tier has a fixed price or allows pay-what-you-can pricing",
    )
    pwyc_min = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Minimum amount for pay-what-you-can pricing",
    )
    pwyc_max = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Maximum amount for pay-what-you-can pricing (optional)",
    )
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY, help_text="ISO 4217 currency code")
    sales_start_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When ticket sales begin for this tier"
    )
    sales_end_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When ticket sales end for this tier"
    )
    total_quantity = models.PositiveIntegerField(default=None, null=True, blank=True)
    quantity_sold = models.PositiveIntegerField(default=0)
    manual_payment_instructions = models.TextField(null=True, blank=True)
    restricted_to_membership_tiers = models.ManyToManyField(
        MembershipTier,
        related_name="restricted_ticket_tiers",
        blank=True,
        help_text="If set, only members of these tiers can purchase this ticket.",
    )

    objects = TicketTierManager()

    def clean(self) -> None:
        """Validate sales window constraints and PWYC fields."""
        super().clean()

        # Validate sales_start_at is before or equal to event start
        if self.sales_start_at and self.sales_start_at > self.event.start:
            raise DjangoValidationError(
                {"sales_start_at": "Ticket sales must start before or at the event start time."}
            )

        # Validate sales_end_at is after sales_start_at
        if self.sales_start_at and self.sales_end_at and self.sales_end_at <= self.sales_start_at:
            raise DjangoValidationError({"sales_end_at": "Ticket sales end time must be after the sales start time."})

        if self.sales_start_at and self.sales_end_at and self.sales_end_at < self.sales_start_at:
            raise DjangoValidationError({"sales_end_at": "Ticket sales end time must be after the sales start time."})

        # Validate PWYC fields
        if self.price_type == self.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise DjangoValidationError(
                    {"pwyc_max": "Maximum pay-what-you-can amount must be greater than or equal to minimum amount."}
                )

        # Check if all selected MembershipTiers belong to the Event's Organization
        for membership_tier in self.restricted_to_membership_tiers.all():
            if membership_tier.organization_id != self.event.organization_id:
                raise DjangoValidationError(
                    {"restricted_to_tiers": "All linked membership tiers must belong to the event's organization."}
                )

        # Enforce logic consistency
        if self.restricted_to_membership_tiers.exists() and self.purchasable_by not in [
            self.PurchasableBy.MEMBERS,
            self.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            raise DjangoValidationError(
                {
                    "restricted_to_tiers": "If tickets are restricted to specific tiers, 'Purchasable By' must be set "
                    "to 'Members only' or 'Invited and Members only'."
                }
            )

    def can_purchase(self) -> bool:
        """Check if the ticket can be purchased."""
        now = timezone.now()
        return (self.sales_start_at is None or self.sales_start_at <= now) and (
            self.sales_end_at is None or self.sales_end_at >= now
        )

    @property
    def total_available(self) -> int | None:
        """Helper property."""
        if self.total_quantity is None:
            return None
        return self.total_quantity - self.quantity_sold

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "name"], name="unique_event_name")]
        indexes = [
            models.Index(
                fields=["id", "event", "payment_method"],
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} for event {self.event.name}"


class TicketQuerySet(models.QuerySet["Ticket"]):
    """Custom queryset for Ticket model with common prefetch patterns."""

    def with_event(self) -> t.Self:
        """Select the related event and its organization."""
        return self.select_related("event", "event__organization")

    def with_tier(self) -> t.Self:
        """Select the related tier."""
        return self.select_related("tier")

    def with_user(self) -> t.Self:
        """Select the related user."""
        return self.select_related("user")

    def full(self) -> t.Self:
        """Select all commonly needed related objects."""
        return self.select_related("event", "event__organization", "tier", "user")


class TicketManager(models.Manager["Ticket"]):
    """Custom manager for Ticket with convenience methods for related object selection."""

    def get_queryset(self) -> TicketQuerySet:
        """Get base queryset."""
        return TicketQuerySet(self.model, using=self._db)

    def with_event(self) -> TicketQuerySet:
        """Returns a queryset with event and organization selected."""
        return self.get_queryset().with_event()

    def with_tier(self) -> TicketQuerySet:
        """Returns a queryset with the tier selected."""
        return self.get_queryset().with_tier()

    def with_user(self) -> TicketQuerySet:
        """Returns a queryset with the user selected."""
        return self.get_queryset().with_user()

    def full(self) -> TicketQuerySet:
        """Returns a queryset with all related objects selected."""
        return self.get_queryset().full()


class Ticket(TimeStampedModel):
    """A ticket for a specific user to a specific event."""

    class TicketStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CHECKED_IN = "checked_in", "Checked In"
        CANCELLED = "cancelled", "Cancelled"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tickets")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets")
    status = models.CharField(max_length=20, choices=TicketStatus.choices, default=TicketStatus.ACTIVE, db_index=True)
    tier = models.ForeignKey(TicketTier, on_delete=models.CASCADE, related_name="tickets")
    checked_in_at = models.DateTimeField(null=True, blank=True, editable=False)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checked_in_tickets",
        editable=False,
    )

    objects = TicketManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user", "tier"],
                condition=models.Q(status="pending"),
                name="unique_ticket_event_user_tier",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        tier_str = f" | {self.tier.name!r}" if self.tier else ""
        return f"Ticket for {self.event.name} for {self.user.username}{tier_str}"

    @cached_property
    def apple_pass_available(self) -> bool:
        """Check if apple pass is available."""
        return bool(
            settings.APPLE_WALLET_PASS_TYPE_ID
            and settings.APPLE_WALLET_TEAM_ID
            and settings.APPLE_WALLET_CERT_PATH
            and settings.APPLE_WALLET_KEY_PATH
            and settings.APPLE_WALLET_WWDR_CERT_PATH
        )


def _get_payment_default_expiry() -> datetime:
    return timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)


class Payment(TimeStampedModel):
    class PaymentStatus(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"

    # note: we cascade on ticket and user deletion because stripe holds financial records for us/the org
    # this is not THE BEST solution, but it's the simplest to keep local GDPR compliance.
    # In the future, a more complex solution will be proposed
    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="payment")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    stripe_session_id = models.CharField(max_length=255, unique=True, db_index=True)
    stripe_payment_intent_id = models.CharField(
        max_length=255, null=True, blank=True, db_index=True, help_text="Stripe PaymentIntent ID for refund processing"
    )
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    raw_response = models.JSONField(blank=True, default=dict)  # To store the full webhook event for auditing
    expires_at = models.DateTimeField(default=_get_payment_default_expiry, db_index=True, editable=False)

    def __str__(self) -> str:
        return f"Payment {self.id} for Ticket {self.ticket.id}"

    def has_expired(self) -> bool:
        """Return whether a payment has expired."""
        return self.expires_at < timezone.now()

    @staticmethod
    def stripe_mode() -> str:
        """Stripe mode."""
        key: str = settings.STRIPE_SECRET_KEY
        return "test" if key.startswith("sk_test_") else "live"

    def stripe_dashboard_url(self) -> str:
        """Return the stripe dashboard URL."""
        mode: str = self.stripe_mode()
        if self.stripe_payment_intent_id:
            return f"https://dashboard.stripe.com/{mode}/payments/{self.stripe_payment_intent_id}"
        return f"https://dashboard.stripe.com/{mode}/checkout/sessions/{self.stripe_session_id}"


class EventInvitationQueryset(models.QuerySet["EventInvitation"]):
    def with_related(self) -> t.Self:
        """Prefetch related objects for invitations."""
        return self.select_related("event", "user", "tier")

    def with_event_details(self) -> t.Self:
        """Prefetch event with its nested relations for list views."""
        return self.select_related(
            "event",
            "event__organization",
            "event__event_series",
            "event__city",
            "user",
            "tier",
        ).prefetch_related(
            Prefetch(
                "event__tags",
                queryset=TagAssignment.objects.select_related("tag"),
                to_attr="prefetched_tagassignments",
            )
        )

    def for_user(self, user: RevelUser) -> t.Self:
        """Get the base invitation qs for a user."""
        today = timezone.now().date()
        return self.filter(Q(user=user) & (Q(event__start__date__gt=today) | Q(event__start__isnull=True)))


class EventInvitationManager(models.Manager["EventInvitation"]):
    def get_queryset(self) -> EventInvitationQueryset:
        """Get base queryset for invitations."""
        return EventInvitationQueryset(self.model, using=self._db)

    def for_user(self, user: RevelUser) -> EventInvitationQueryset:
        """Get the base invitation qs for a user."""
        return self.get_queryset().for_user(user)

    def with_related(self) -> EventInvitationQueryset:
        """Returns a queryset with related objects."""
        return self.get_queryset().with_related()

    def with_event_details(self) -> EventInvitationQueryset:
        """Returns a queryset with event and nested relations."""
        return self.get_queryset().with_event_details()


class AbstractEventInvitation(TimeStampedModel):
    """Abstract base class for event invitations with common fields."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    waives_questionnaire = models.BooleanField(default=False)
    waives_purchase = models.BooleanField(default=False)
    overrides_max_attendees = models.BooleanField(default=False)
    waives_membership_required = models.BooleanField(default=False)
    waives_rsvp_deadline = models.BooleanField(default=False, help_text="Waives RSVP deadline check for this user")
    custom_message = models.TextField(null=True, blank=True)
    tier = models.ForeignKey(TicketTier, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        abstract = True


class EventInvitation(AbstractEventInvitation):
    """Event invitation for registered users."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invitations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invitations")

    objects = EventInvitationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_invitation_event_user",
            )
        ]
        ordering = ["-created_at"]


class PendingEventInvitation(AbstractEventInvitation):
    """Event invitation for unregistered users (by email)."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pending_invitations")
    email = models.EmailField(db_index=True, help_text="Email of the user to be invited")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "email"],
                name="unique_pending_event_invitation_event_email",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Pending invitation for {self.email} to {self.event.name}"


class EventRSVP(TimeStampedModel):
    class RsvpStatus(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        MAYBE = "maybe", "Maybe"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rsvps")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rsvps")
    status = models.CharField(
        max_length=20, choices=RsvpStatus.choices, default=None, null=True, blank=True, db_index=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_user",
            )
        ]


class EventToken(TokenMixin):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tokens")
    grants_invitation = models.BooleanField(default=True)
    ticket_tier = models.ForeignKey(TicketTier, on_delete=models.CASCADE, null=True, blank=True)
    invitation_payload = models.JSONField(
        null=True, blank=True, help_text="If provided, the token will we viable to claim invitations."
    )

    class Meta:
        indexes = [
            # For listing active tokens by event
            models.Index(fields=["event", "expires_at"], name="eventtoken_event_expires"),
            # For listing tokens by event ordered by creation
            models.Index(fields=["event", "-created_at"], name="eventtoken_event_created"),
        ]
        ordering = ["-created_at"]


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


class EventInvitationRequest(UserRequestMixin):
    class InvitationRequestStatus(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invitation_requests")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_user_invitation_request",
            )
        ]
        ordering = ["-created_at"]


class PotluckItem(TimeStampedModel):
    class ItemTypes(models.TextChoices):
        FOOD = "food", "Food"
        MAIN_COURSE = "main_course", "Main Course"
        SIDE_DISH = "side_dish", "Side Dish"
        DESSERT = "dessert", "Dessert"
        DRINK = "drink", "Drink"
        ALCOHOL = "alcohol", "Alcohol"
        NON_ALCOHOLIC = "non_alcoholic", "Non-Alcoholic"
        SUPPLIES = "supplies", "Supplies"  # cups, napkins, etc.
        LABOR = "labor", "Labor / Help"  # setup, cleanup, etc.
        ENTERTAINMENT = "entertainment", "Entertainment"  # music, games, performance
        SEXUAL_HEALTH = "sexual_health", "Sexual Health"  # condoms, lube, gloves
        TOYS = "toys", "Toys"
        CARE = "care", "Care"  # blankets, snacks, water, comfort stuff
        TRANSPORT = "transport", "Transport / Shuttle"  # offer a ride etc.
        MISC = "misc", "Miscellaneous"

    created_by = models.ForeignKey(RevelUser, on_delete=models.SET_NULL, null=True, blank=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="potluck_items")
    name = models.CharField(max_length=100, db_index=True)
    quantity = models.CharField(max_length=20, blank=True, null=True)
    item_type = models.CharField(choices=ItemTypes.choices, max_length=20, db_index=True)
    note = MarkdownField(null=True, blank=True)
    is_suggested = models.BooleanField(default=False, help_text="For host-created items awaiting volunteers")
    assignee = models.ForeignKey(
        RevelUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="potluck_items"
    )
