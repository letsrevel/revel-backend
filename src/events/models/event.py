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

from accounts.models import RevelUser
from common.models import TagAssignment, TaggableMixin, TimeStampedModel

from .event_series import EventSeries
from .mixins import (
    LocationMixin,
    LogoCoverValidationMixin,
    SlugFromNameMixin,
    TokenMixin,
    UserRequestMixin,
    VisibilityMixin,
)
from .organization import Organization, OrganizationMember


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
        """Get the queryset based on the user, using an efficient subquery strategy."""
        base_qs = self.select_related("organization", "event_series", "city")

        is_allowed_special = Q(id__in=allowed_ids) if allowed_ids else Q()

        if not include_past:
            today = timezone.now().date()
            base_qs = base_qs.filter(Q(start__date__gte=today) | Q(start__isnull=True) | is_allowed_special)

        if user.is_superuser or user.is_staff:
            return base_qs

        if user.is_anonymous:
            return base_qs.filter(
                Q(visibility=Event.Visibility.PUBLIC, status__in=[Event.Status.OPEN, Event.Status.CLOSED])
                | is_allowed_special
            )

        # --- Subquery Strategy ---
        # 1. Get IDs of all non-public events this user has a specific relationship with.

        # Events they are invited to
        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)

        # Events where they are a member of the organization
        member_org_ids = OrganizationMember.objects.filter(user=user).values_list("organization_id", flat=True)
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
        is_public = Q(visibility=Event.Visibility.PUBLIC)
        is_allowed_non_public = Q(id__in=list(allowed_non_public_ids))

        # Users see events if they are public, if they are staff/owner,
        # or if they have a specific permission (invite/member)
        final_qs = base_qs.filter(is_public | is_owner_or_staff | is_allowed_non_public)

        # Only staff/owners can see drafts
        if not (user.is_staff or user.is_superuser):
            final_qs = final_qs.exclude(~is_owner_or_staff & Q(status=Event.Status.DRAFT))

        return final_qs.distinct()


class EventManager(models.Manager["Event"]):
    def get_queryset(self) -> EventQuerySet:
        """Get base queryset for events."""
        return EventQuerySet(self.model, using=self._db).with_tags()

    def with_organization(self) -> EventQuerySet:
        """Returns a queryset prefetching an organization and its members."""
        return self.get_queryset().with_organization()

    def with_city(self) -> EventQuerySet:
        """Returns a queryset selecting the related city for the events."""
        return self.get_queryset().with_city()

    def for_user(
        self, user: RevelUser | AnonymousUser, include_past: bool = False, allowed_ids: list[UUID] | None = None
    ) -> EventQuerySet:
        """Get the queryset based on the user."""
        return self.get_queryset().for_user(user, include_past=include_past, allowed_ids=allowed_ids)


class Event(
    SlugFromNameMixin, TimeStampedModel, VisibilityMixin, LocationMixin, LogoCoverValidationMixin, TaggableMixin
):
    class Types(models.TextChoices):
        PUBLIC = "public"
        PRIVATE = "private"
        MEMBERS_ONLY = "members-only"

    class Status(models.TextChoices):
        OPEN = "open"
        CLOSED = "closed"
        DRAFT = "draft"
        DELETED = "deleted"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="events")
    status = models.CharField(choices=Status.choices, max_length=10, default=Status.DRAFT)
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)
    description = models.TextField(blank=True, null=True)
    invitation_message = models.TextField(
        blank=True,
        null=True,
        help_text="Invitation message to override the one automatically generated using name and description.",
    )
    event_type = models.CharField(choices=Types.choices, max_length=20, db_index=True, default=Types.PRIVATE)
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
    free_for_members = models.BooleanField(default=False)
    free_for_staff = models.BooleanField(default=True)
    requires_ticket = models.BooleanField(default=True)  # If False, managed via RSVPs
    potluck_open = models.BooleanField(default=False)

    attendee_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="unique_organization_name"),
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

    def attendees(self, viewer: RevelUser) -> models.QuerySet[RevelUser]:
        """Return attendees based on who wants to see them."""
        if (
            viewer.is_superuser
            or viewer.is_staff
            or self.organization.owner_id == viewer.id
            or self.organization.staff_members.filter(id=viewer.id).exists()
        ):
            return RevelUser.objects.filter(
                Q(tickets__event=self, tickets__status=Ticket.Status.ACTIVE)
                | Q(rsvps__event=self, rsvps__status=EventRSVP.Status.YES)
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
        if self.end < self.start:
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
        if not self.status == self.Status.OPEN:
            return False

        # If check-in window is explicitly defined, use it
        if self.check_in_starts_at and self.check_in_ends_at:
            return self.check_in_starts_at <= now <= self.check_in_ends_at

        # If no check-in window is defined, default to event start/end times
        return self.start <= now <= self.end

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
        return str(c).encode("utf-8")


class EventWaitList(TimeStampedModel):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "user"], name="unique_event_waitlist")]
        ordering = ["-created_at"]


DEFAULT_TICKET_TIER_NAME = "General Admission"


class TicketTierQuerySet(models.QuerySet["TicketTier"]):
    def for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Return ticket tiers visible to a given user, combining event and tier-level access."""
        qs = self.select_related("event", "event__organization")

        # --- Anonymous User ---
        if user.is_anonymous:
            return qs.filter(
                visibility=TicketTier.Visibility.PUBLIC,
                event__visibility=Event.Visibility.PUBLIC,
                event__status=Event.Status.OPEN,
            )

        if user.is_superuser:
            return qs

        # --- Authenticated User ---
        # 1. Get all events this user is allowed to see. This is the source of truth.
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

        member_org_ids = OrganizationMember.objects.filter(user=user).values_list("organization_id", flat=True)
        is_member_tier = Q(
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            event__organization_id__in=member_org_ids,
        )

        invited_event_ids = EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)
        is_private_tier = Q(visibility=TicketTier.Visibility.PRIVATE, event_id__in=invited_event_ids)

        # A regular user can see a tier if it's on a visible event AND...
        # ...it's a public tier, OR
        # ...it's a member tier and they are a member, OR
        # ...it's a private tier and they are invited.
        tier_visibility_q = is_public_tier | is_member_tier | is_private_tier

        # Staff/owners of other orgs might also see some of these events if public,
        # so we also explicitly include tiers for events they are staff/owner of.
        final_q = base_q & (tier_visibility_q | is_owner_or_staff)

        return qs.filter(final_q).distinct()


class TicketTierManager(models.Manager["TicketTier"]):
    def get_queryset(self) -> TicketTierQuerySet:
        """Get a QS for ticket tiers."""
        return TicketTierQuerySet(self.model, using=self._db)

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
        default=PaymentMethod.ONLINE,
        max_length=20,
        db_index=True,
    )
    purchasable_by = models.CharField(
        choices=PurchasableBy.choices, max_length=20, db_index=True, default=PurchasableBy.PUBLIC
    )
    description = models.TextField(null=True, blank=True)
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


class Ticket(TimeStampedModel):
    """A ticket for a specific user to a specific event."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CHECKED_IN = "checked_in", "Checked In"
        CANCELLED = "cancelled", "Cancelled"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tickets")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True)
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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user", "tier"],
                condition=models.Q(status="pending"),
                name="unique_ticket_event_user_tier",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        tier_str = f" | {self.tier.name!r}" if self.tier else ""
        return f"Ticket for {self.event.name} for {self.user.username}{tier_str}"


def _get_payment_default_expiry() -> datetime:
    return timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)


class Payment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"

    ticket = models.OneToOneField(Ticket, on_delete=models.PROTECT, related_name="payment")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    stripe_session_id = models.CharField(max_length=255, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
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


class EventInvitationQueryset(models.QuerySet["EventInvitation"]):
    def get_queryset(self) -> t.Self:
        """Get the base invitation queryset."""
        return self.select_related("event", "user", "tier")

    def for_user(self, user: RevelUser) -> t.Self:
        """Get the base invitation qs for a user."""
        today = timezone.now().date()
        return self.filter(Q(user=user) & (Q(event__start__date__gt=today) | Q(event__start__isnull=True)))


class EventInvitationManager(models.Manager["EventInvitation"]):
    def get_queryset(self) -> EventInvitationQueryset:
        """Get base queryset for events."""
        return EventInvitationQueryset(self.model, using=self._db)

    def for_user(self, user: RevelUser) -> EventInvitationQueryset:
        """Get the base invitation qs for a user."""
        return self.get_queryset().for_user(user)


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

    def __str__(self) -> str:
        return f"Pending invitation for {self.email} to {self.event.name}"


class EventRSVP(TimeStampedModel):
    class Status(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        MAYBE = "maybe", "Maybe"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rsvps")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rsvps")
    status = models.CharField(max_length=20, choices=Status.choices, default=None, null=True, blank=True, db_index=True)

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
    invitation_tier = models.ForeignKey(TicketTier, on_delete=models.SET_NULL, null=True, blank=True)
    invitation_payload = models.JSONField(
        null=True, blank=True, help_text="If provided, the token will we viable to claim invitations."
    )


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
    class Status(models.TextChoices):
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
    note = models.TextField(null=True, blank=True)
    is_suggested = models.BooleanField(default=False, help_text="For host-created items awaiting volunteers")
    assignee = models.ForeignKey(
        RevelUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="potluck_items"
    )
